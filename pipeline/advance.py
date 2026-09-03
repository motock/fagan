"""Advance-pipeline tick logic, split verbatim from pipeline/server.py.

The two functions here (_advance_pipeline_locked and
_advance_pipeline_locked_impl) were moved out of pipeline/server.py without
any change to their bodies. They are re-exported onto pipeline.server so
existing monkeypatch targets and call sites keep resolving.

The test suite monkeypatches module globals on ``pipeline.server`` (e.g.
``p.PIPELINE_AUTONOMY``, ``p.dispatch_story``, ``p._merge_pr``), so every
server-sourced name these functions read is resolved via a ``_ServerRef``
binding that delegates to the *current* ``pipeline.server`` namespace at call
time - the same circular-avoidance pattern used by pipeline/service.py,
pipeline/dispatch.py and pipeline/merge.py.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .wedge_io import run_wedge_scan


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name."""

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        from . import server as _server
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other

    def __contains__(self, item):
        return item in self._value()

    def __iter__(self):
        return iter(self._value())

    def __sub__(self, other):
        return self._value() - other

    def __eq__(self, other):
        return self._value() == other

    def __ne__(self, other):
        return self._value() != other

    def __lt__(self, other):
        return self._value() < other

    def __le__(self, other):
        return self._value() <= other

    def __gt__(self, other):
        return self._value() > other

    def __ge__(self, other):
        return self._value() >= other

    def __bool__(self):
        return bool(self._value())

    def __len__(self):
        return len(self._value())

    def __getitem__(self, key):
        return self._value()[key]


# Server-sourced members the moved functions reference as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
DISPATCH_MAX_ATTEMPTS = _ServerRef("DISPATCH_MAX_ATTEMPTS")
MAX_CONCURRENT_AGENTS = _ServerRef("MAX_CONCURRENT_AGENTS")
MERGE_MAX_ATTEMPTS = _ServerRef("MERGE_MAX_ATTEMPTS")
PIPELINE_AUTONOMY = _ServerRef("PIPELINE_AUTONOMY")
_atomic_write_json = _ServerRef("_atomic_write_json")
_auto_escalation_enabled = _ServerRef("_auto_escalation_enabled")
_ci_pending_expired = _ServerRef("_ci_pending_expired")
_ci_rerun = _ServerRef("_ci_rerun")
_ci_rework_feedback = _ServerRef("_ci_rework_feedback")
_completed_dep_ids = _ServerRef("_completed_dep_ids")
_count_in_progress_agents = _ServerRef("_count_in_progress_agents")
# The on-device slot count scans every plan's manifest (the spend window the
# cap protects is session-wide), so it reads PLAN_DIR as a free var exactly
# like _count_in_progress_agents does in pipeline/concurrency.py. Resolving
# it via a _ServerRef keeps the plan_dir fixture's p.PLAN_DIR patch landing.
PLAN_DIR = _ServerRef("PLAN_DIR")
_default_branch = _ServerRef("_default_branch")
_escalate_to_claude = _ServerRef("_escalate_to_claude")
_escalate_to_local_fallback_model = _ServerRef("_escalate_to_local_fallback_model")
_escalation_label = _ServerRef("_escalation_label")
_mark_plane_done = _ServerRef("_mark_plane_done")
_maybe_record_retro = _ServerRef("_maybe_record_retro")
_mcp_restart_notice = _ServerRef("_mcp_restart_notice")
_mcp_self_source_touched = _ServerRef("_mcp_self_source_touched")
_merge_decision = _ServerRef("_merge_decision")
def _degraded_ci_branch(key: str) -> str:
    """CI-poll branch for a story whose worktree cannot be probed.

    Nothing was dispatched, so no alias agent/<key>-<suffix> can exist and
    the merge gate is handed NO branch at all - a locally computed
    convention branch reaching ``_rebase_and_push_for_merge`` is the exact
    mistake the round-2 review finding names. The CI poll, however, must
    still run (the ci-pending/cancelled contracts pin it, see
    test_advance_pipeline_cancelled_ci_triggers_one_rerun_then_merges),
    and with no worktree to probe the convention name is the only branch
    the story could ever have had. This name feeds the poll ONLY - it never
    reaches the gate, so it cannot be pushed or merged. Polling an empty
    branch instead would make ``gh pr checks ""`` resolve the CURRENT
    checkout's PR and import an unrelated CI verdict into this story's
    merge decision.
    """
    from .pr import _convention_branch

    return _convention_branch(key)


_merge_gate_ci_status = _ServerRef("_merge_gate_ci_status")
_merge_pr = _ServerRef("_merge_pr")
_notify_user = _ServerRef("_notify_user")
_rebase_and_push_for_merge = _ServerRef("_rebase_and_push_for_merge")
_resolve_dispatch_backend = _ServerRef("_resolve_dispatch_backend")
_reverify_acceptance = _ServerRef("_reverify_acceptance")
_reverify_build = _ServerRef("_reverify_build")
_role_resource_ok = _ServerRef("_role_resource_ok")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_store = _ServerRef("_store")
backend = _ServerRef("backend")
check_story_status = _ServerRef("check_story_status")
dispatch_story = _ServerRef("dispatch_story")
interrupt_story = _ServerRef("interrupt_story")
review_story = _ServerRef("review_story")
run_triage_sweep = _ServerRef("run_triage_sweep")

# _advance_pipeline_locked delegates to _advance_pipeline_locked_impl. The
# triage-sweep tests monkeypatch p._advance_pipeline_locked_impl, so resolve it
# from pipeline.server at call time (the real implementation lives below).
_impl_ref = _ServerRef("_advance_pipeline_locked_impl")


def _advance_pipeline_locked(plan_name: str) -> dict[str, Any]:
    """Run the advisory pre-tick sweeps (failure triage, wedge scan), then the
    tick proper. Rename-and-delegate: the tick body moved verbatim to
    _advance_pipeline_locked_impl; both sweeps are advisory and fail-open (C2):
    anything that goes wrong inside them must leave the tick exactly as it was.
    """
    try:
        run_triage_sweep(plan_name)
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning(
            "triage sweep raised; continuing with the tick unchanged"
        )
    try:
        run_wedge_scan(plan_name)
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning(
            "wedge scan raised; continuing with the tick unchanged"
        )
    return _impl_ref(plan_name)


def _story_dispatch_is_on_device(story: dict[str, Any]) -> bool:
    """True when a story's dispatch has an on-device footprint.

    A story is slot-exempt (returns False) when its dispatch is cloud-backed:
    either its model tag is explicitly ``:cloud`` (served via Ollama with zero
    local VRAM footprint) or it is claude-routed (gated by the usage pause
    thresholds in the per-story gate loop, not by this cap).

    Conservative on the ambiguous cases, mirroring the per-story gate's
    no-tag branch: a story with NO explicit model tag resolves to the
    env-default on-device model, so it COUNTS. Likewise a story with no
    explicit ``backend`` field resolves to the env-default backend, so it
    counts even when that default is ``claude`` — only an explicitly
    claude-routed story (``story["backend"] == "claude"``) is exempt.
    """
    tag = story.get("model")
    if tag and tag.endswith(":cloud"):
        return False
    return story.get("backend") != "claude"


def _count_on_device_in_progress_agents() -> int:
    """Count *actually running* ON-DEVICE dispatched agents across every
    plan's manifest — the count MAX_CONCURRENT_AGENTS slots are sized
    against.

    Same cross-plan scope, pid-liveness check and pure-read contract as
    ``_count_in_progress_agents`` (which stays backend-blind for its other
    callers in pipeline/dispatch.py); only the per-story filter differs: a
    story counts unless its dispatch is cloud-backed (see
    _story_dispatch_is_on_device).
    """
    count = 0
    for manifest_path in PLAN_DIR.glob("*.manifest.json"):
        manifest = json.loads(manifest_path.read_text())
        for story in manifest.get("stories", {}).values():
            if story.get("status") != "in_progress" or "pid" not in story:
                continue
            try:
                os.kill(story["pid"], 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            if _story_dispatch_is_on_device(story):
                count += 1
    return count


def _advance_pipeline_locked_impl(plan_name: str) -> dict[str, Any]:
    manifest_path = _store.manifest_path(plan_name)
    if not manifest_path.exists():
        return {"ok": False, "error": f"No manifest for {plan_name}"}
    manifest = _store.get_manifest(plan_name)
    stories = manifest["stories"]

    if manifest.get("paused"):
        # A human-requested pause for this one plan: unlike the usage gate,
        # this doesn't even adjudicate merges - the plan should sit
        # completely still until explicitly resumed. Still free up any
        # running agent so a paused plan isn't quietly burning usage.
        with _scoped_repo_root(plan_name):
            for key, story in stories.items():
                if story["status"] == "in_progress" and "pid" in story:
                    interrupt_story(plan_name, key)
        return {"ok": True, "skipped": "plan_paused"}

    # Per-backend resource gate (Step 5): dispatch and review can run on
    # different backends, so gate each by ITS backend's availability rather
    # than one global Claude flag. This is what lets local dispatch keep
    # running when Claude's weekly limit is hit (and vice versa).
    dispatch_ok, _dispatch_reason = _role_resource_ok("dispatch")
    review_ok, review_reason = _role_resource_ok(
        "review", plan_role_config=manifest.get("role_config")
    )

    done = _completed_dep_ids(stories)
    ready = [
        k
        for k, v in stories.items()
        if v["status"] in ("todo", "interrupted", "changes_requested")
        and all(d in done for d in v.get("dependencies", []))
    ]

    # Dereference once: PIPELINE_AUTONOMY is a _ServerRef proxy (comparisons
    # like `== "dry-run"` work via its __eq__), but the raw proxy object is
    # not JSON-serializable, so anything embedded in the returned dict must
    # use the resolved plain string instead.
    autonomy = PIPELINE_AUTONOMY._value()

    if autonomy == "dry-run":
        return {
            "ok": True,
            "dry_run": True,
            "autonomy": autonomy,
            # "paused" kept for back-compat = dispatch gated.
            "paused": not dispatch_ok,
            "dispatch_paused": not dispatch_ok,
            "review_paused": not review_ok,
            "would_dispatch": ready if dispatch_ok else [],
            "would_merge_decisions": {
                k: _merge_decision(v)
                for k, v in stories.items()
                if v["status"] == "pr_open"
            },
        }

    summary: dict[str, Any] = {
        "autonomy": autonomy,
        "paused": not dispatch_ok,
        "dispatch_paused": not dispatch_ok,
        "review_paused": not review_ok,
        "dispatched": [],
        "advanced": [],
        "merged": [],
        "parked": [],
        "failed": [],
        "interrupted": [],
        "notify": [],
        "review_deferred": [],
        "ci_pending": [],
    }

    # Scoped for the whole tick: dispatch_story resolves its own repo_root
    # too (so it's correct called standalone), but _merge_pr and
    # _default_branch read the plain REPO_ROOT global, so this plan's repo
    # must be active for the duration of every action below.
    with _scoped_repo_root(plan_name):
        # Per-story dispatch gate. The dispatch backend is resolved per-story
        # (dispatch_story's own resolution, shared via _resolve_dispatch_backend),
        # so the gate must be per-story too: a :cloud-tagged model (served via
        # Ollama with zero local VRAM footprint) or a Claude-routed story must
        # never be blocked by the LOCAL free-memory floor, while an on-device
        # model keeps the floor exactly as before. Reachability still applies
        # to every backend (a cloud model proxied through an unreachable server
        # cannot dispatch).
        env_backend = (
            os.environ.get("PIPELINE_BACKEND_DISPATCH", "claude").strip().lower()
        )
        # In-progress interruption is also per-story: only interrupt an
        # in-progress story whose OWN backend+model would be gated by the
        # LOCAL memory floor (local + non-:cloud). A :cloud or claude-routed
        # in-progress story must NOT be interrupted by the local memory gate.
        # The blanket gate's memory-pressure exception (do not interrupt on
        # 'insufficient free memory') is preserved for the claude-routed path
        # (a claude story is only interrupted when the blanket gate is down
        # for a NON-memory reason, e.g. Claude usage exhausted).
        memory_pressure = "insufficient free memory" in _dispatch_reason
        for key, story in stories.items():
            if story["status"] != "in_progress" or "pid" not in story:
                continue
            story_backend = _resolve_dispatch_backend(story, env_backend)
            if story_backend == "claude":
                # Claude-routed: interrupt only when the blanket gate is down
                # for a non-memory reason (Claude usage exhausted, etc.).
                # Never interrupt a claude story on local memory pressure.
                if not dispatch_ok and not memory_pressure:
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
                continue
            # Local backend: gate on the local memory floor for THIS story's
            # model. A :cloud tag has no local footprint, so it is never
            # interrupted by the memory gate. An on-device tag is interrupted
            # when its own floor is not met. A story with no explicit model
            # resolves to the env-default on-device model, which the blanket
            # gate already accounts for - interrupt it only when the blanket
            # gate is down for a non-memory reason (preserving old behavior).
            tag = story.get("model")
            if tag and tag.endswith(":cloud"):
                continue
            if tag:
                status = backend.get_backend("dispatch", name=story_backend).resource_status(
                    model_tag=tag
                )
                if not status.get("ok", True):
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
            else:
                if not dispatch_ok and not memory_pressure:
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)

        # 1. Dispatch ready (and resumable-interrupted) stories, capped to
        # the ON-DEVICE slots still free under MAX_CONCURRENT_AGENTS. <=0
        # means no cap. Only on-device dispatch consumes a slot: cloud-backed
        # dispatch (a :cloud-tagged model or a claude-routed story) has no
        # on-device footprint to protect, so it bypasses the cap entirely —
        # its spend is bounded by the usage pause thresholds in the per-story
        # gate below instead. _count_in_progress_agents keeps its backend-
        # blind cross-plan semantics for its other callers
        # (pipeline/dispatch.py's concurrent-dispatch warning gates); only
        # this cap's slot math gets the on-device-aware count.
        if MAX_CONCURRENT_AGENTS > 0:
            free_device_slots = max(
                0, MAX_CONCURRENT_AGENTS - _count_on_device_in_progress_agents()
            )
            capped = True
        else:
            free_device_slots = 0
            capped = False
        # Per-story dispatch gate: only dispatch stories whose own backend+model
        # passes its gate. A :cloud story dispatches even under local memory
        # pressure; an on-device story is deferred when the floor is not met.
        # Independent of the cap: a slot-exempt story still runs this gate,
        # and gated stories are recorded exactly as before.
        gated = []
        for key in ready:
            story = stories[key]
            on_device = _story_dispatch_is_on_device(story)
            if capped and on_device and free_device_slots <= 0:
                # On-device slots are exhausted: defer this story until a
                # slot frees up. Cloud dispatches never reach this branch.
                continue
            story_backend = _resolve_dispatch_backend(story, env_backend)
            if story_backend == "claude":
                # Gate on Claude's usage resource_status, NOT local memory.
                status = backend.get_backend("dispatch", name="claude").resource_status()
                if not status.get("ok", True):
                    gated.append(key)
                    continue
            else:
                tag = story.get("model")
                if tag:
                    status = backend.get_backend("dispatch", name=story_backend).resource_status(
                        model_tag=tag
                    )
                else:
                    status = backend.get_backend("dispatch", name=story_backend).resource_status()
                if not status.get("ok", True):
                    gated.append(key)
                    continue
            if capped and on_device:
                # Only a dispatch that actually launches on-device consumes
                # the slot; a cloud dispatch must not touch the counter.
                free_device_slots -= 1
            try:
                result = dispatch_story(plan_name, key)
                if not (isinstance(result, dict) and result.get("skipped")):
                    summary["dispatched"].append(key)
                else:
                    summary.setdefault("skipped", []).append(key)
            except Exception as e:  # noqa: BLE001 (git fetch/worktree/backend launch failure)
                # Re-read: dispatch_story only writes the manifest on a
                # successful launch, so on a raise the on-disk status is
                # still todo/interrupted - bump the attempt counter there.
                m = json.loads(manifest_path.read_text())
                st = m["stories"][key]
                attempts = st.get("dispatch_attempts", 0) + 1
                st["dispatch_attempts"] = attempts
                # W4L-04: stamp the story's dispatch-failure notifications with
                # its persisted correlation_id (omitted entirely for older
                # manifests). The attempt counter is already in scope here.
                _cid_kwargs = (
                    {
                        "correlation_id": st["correlation_id"],
                        "attempt": st.get("dispatch_attempts", 0),
                    }
                    if st.get("correlation_id")
                    else {}
                )
                if attempts >= DISPATCH_MAX_ATTEMPTS:
                    st["status"] = "failed"
                    st["dispatch_error"] = str(e)
                    _notify_user(
                        plan_name,
                        f"{key} dispatch failed {attempts}x "
                        f"({e}); giving up - needs human intervention.",
                        event="dispatch_failed",
                        **_cid_kwargs,
                    )
                    summary["failed"].append(key)
                else:
                    # leave status dispatch-eligible; the next tick retries.
                    _notify_user(
                        plan_name,
                        f"{key} dispatch attempt {attempts}/"
                        f"{DISPATCH_MAX_ATTEMPTS} failed ({e}); will retry.",
                        **_cid_kwargs,
                    )
                summary["notify"].append(key)
                _atomic_write_json(manifest_path, m)

        # 2. Poll running agents: tests fail -> notify (or escalate); tests pass
        # -> tests_passed. Polling is per-story, mirroring the interruption gate
        # above: a story this tick would interrupt (blanket gate down for a
        # non-memory reason, or its own memory floor not met) is NOT polled; a
        # surviving story IS polled even when the blanket dispatch gate is down.
        # This keeps a :cloud story dispatched under local memory pressure from
        # stalling in_progress unpolled — dispatch and interruption are already
        # per-story, so polling must be too. Review below stays gated by the
        # REVIEW backend (review_ok); merge (further below) is unconditional.
        manifest = json.loads(manifest_path.read_text())
        stories = manifest["stories"]
        for key, story in stories.items():
            if story["status"] != "in_progress" or "pid" not in story:
                continue
            # Per-story poll gate — mirrors the interruption gate (lines above):
            # poll only stories that survive this tick (were NOT interrupted).
            story_backend = _resolve_dispatch_backend(story, env_backend)
            tag = story.get("model")
            if story_backend != "claude" and tag and tag.endswith(":cloud"):
                pass  # :cloud is never interrupted by the local memory gate
            elif story_backend == "claude" or not tag:
                # Interrupted when the blanket gate is down for a non-memory
                # reason; polled when the gate is up OR down for memory pressure.
                if not (dispatch_ok or memory_pressure):
                    continue
            else:
                # On-device with an explicit model tag: polled only when its own
                # floor holds (otherwise it was interrupted this tick).
                if not backend.get_backend("dispatch", name=story_backend).resource_status(
                    model_tag=tag
                ).get("ok", True):
                    continue
            check_result = check_story_status(plan_name, key)
            status = check_result.get("status")
            if status == "failed":
                fallback_model = manifest.get("local_model_fallback")
                # A-posteriori escalation: under auto dispatch, if the
                # local agent failed and has NOT been escalated before,
                # wipe its worktree and re-queue for Claude. A second
                # failure (on Claude), or any failure under an explicit
                # non-auto backend, is terminal.
                if (
                    _auto_escalation_enabled()
                    and story.get("backend") == "local"
                    and not story.get("escalated")
                ):
                    manifest = json.loads(manifest_path.read_text())
                    _escalate_to_claude(manifest, plan_name, key, manifest_path)
                    _notify_user(
                        plan_name,
                        f"{key} local agent failed; escalating to {_escalation_label()} and starting clean.",
                        event="escalated",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["notify"].append(key)
                elif (
                    fallback_model
                    and story.get("backend") == "local"
                    and story.get("model") != fallback_model
                    and not story.get("tried_fallback_model")
                ):
                    # Plan-scoped opt-in (manifest["local_model_fallback"]):
                    # never escalates to Claude - just gives one other
                    # local model a shot before the terminal park/fail
                    # path below.
                    manifest = json.loads(manifest_path.read_text())
                    failed_model = (
                        story.get("dispatched_model")
                        or story.get("model")
                        or "default"
                    )
                    _escalate_to_local_fallback_model(
                        manifest, plan_name, key, manifest_path, fallback_model
                    )
                    _notify_user(
                        plan_name,
                        f"{key} local agent failed on {failed_model}; retrying on "
                        f"fallback model {fallback_model} before parking.",
                        event="model_fallback",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["notify"].append(key)
                elif check_result.get("failure_kind") == "give_up":
                    # T6: the agent explicitly surrendered rather than
                    # producing ordinary red tests. Point the human at
                    # the story's scope/clarity instead of the generic
                    # message - a missing/wrong API needs a fix to
                    # agent_instructions, not another identical retry.
                    _notify_user(
                        plan_name,
                        f"{key} agent gave up (explicit surrender, zero productive "
                        f"progress) - likely under-specified (missing API, wrong "
                        f"scope) rather than a model-capability gap; needs human "
                        f"clarification before another dispatch.",
                        event="agent_gave_up",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["failed"].append(key)
                    summary["notify"].append(key)
                else:
                    _notify_user(
                        plan_name,
                        f"{key} tests failed",
                        event="tests_failed",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["failed"].append(key)
                    summary["notify"].append(key)

        # Review every tests_passed story (incl. ones orphaned by a crashed
        # review on a prior tick - review_story is idempotent). Gated by the
        # REVIEW backend independently of dispatch: a Claude-dispatch pause no
        # longer blocks reviewing already-finished work on a healthy review
        # backend, and a local-dispatch run can still defer review if review
        # is on Claude and Claude is gated.
        if review_ok:
            stories = json.loads(manifest_path.read_text())["stories"]
            for key, story in stories.items():
                if story["status"] == "tests_passed":
                    rv = review_story(plan_name, key)
                    summary["advanced"].append({key: rv["status"]})
                    if rv.get("deferred") == "rate_limited":
                        summary["review_deferred"].append(key)
        else:
            # Only surface the review gate when there is tests_passed work
            # waiting to be reviewed this tick; otherwise the notification is
            # noise (e.g. all stories are already pr_open awaiting merge).
            if any(s["status"] == "tests_passed" for s in stories.values()):
                _notify_user(
                    plan_name, f"Review backend gated ({review_reason}): deferring review."
                )
                summary["notify"].append("review_paused")

        # 3. Adjudicate merges for reviewed PRs (no model usage; runs even paused).
        manifest = json.loads(manifest_path.read_text())
        stories = manifest["stories"]
        for key, story in stories.items():
            if story["status"] != "pr_open":
                continue
            decision = _merge_decision(story)
            if decision["action"] != "merge":
                story["status"] = "parked"
                story["parked_reason"] = decision["reason"]
                _notify_user(
                    plan_name,
                    f"{key} parked: {decision['reason']}",
                    **(
                        {"correlation_id": story["correlation_id"]}
                        if story.get("correlation_id")
                        else {}
                    ),
                )
                summary["parked"].append(key)
                summary["notify"].append(key)
                continue

            # Mode 9: rebase onto current origin/master + CI gate before merge,
            # so a stale-base branch can't land cross-story breakage or a
            # ruff-red PR onto main. Failures count against merge_attempts just
            # like a transient `gh pr merge` failure (see MERGE_MAX_ATTEMPTS).
            worktree = story.get("worktree", "")
            # Resolve the worktree's ACTUAL HEAD branch (a rework round can
            # leave it on an alias agent/<key>-<suffix>) so the gate's rebase,
            # push, CI poll and _merge_pr all operate on the one branch
            # _merge_pr merges. The hardcoded convention name previously
            # named a branch a prior _merge_pr had already deleted ("src
            # refspec ... does not match any") or a stale twin, and the CI
            # poll queried a SHA that was never pushed to it. The resolver
            # itself fails open to the convention name when the worktree
            # cannot be probed, so no local fallback is needed here - and
            # none may be added: a locally computed convention branch is the
            # exact mistake the round-2 review finding names.
            from .pr import _resolve_story_branch

            if worktree and Path(worktree).is_dir():
                branch = _resolve_story_branch(worktree, key)
            else:
                # No worktree to probe (missing/anomalous): nothing was
                # dispatched, so no alias can exist. Hand the gate NO
                # branch at all - a locally computed convention branch is
                # the exact mistake the round-2 review finding names, and
                # the gate's own is_dir guard skips rebase/push for a
                # missing worktree without spawning a subprocess (the
                # CI-gate-disabled path must run zero subprocesses, see
                # test_advance_pipeline_ci_gate_disabled_skips_ci).
                branch = ""
            gate_error = ""
            ci_definitive_fail = False
            ci_wait = False
            # S5: a story already polling a pending CI run must not
            # re-rebase/force-push on every tick - with the non-blocking CI
            # poll that mints a new SHA whenever origin/master moved,
            # restarting CI and burning Actions minutes. Skip straight to
            # polling the exact SHA recorded on the first pending observation.
            if story.get("ci_pending_sha"):
                pushed_sha = story["ci_pending_sha"]
            else:
                gate_error, pushed_sha = _rebase_and_push_for_merge(plan_name, key, branch, worktree)
            if not gate_error:
                poll_branch = branch or _degraded_ci_branch(key)
                ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha)
                if ci["state"] == "cancelled" and not story.get(
                    "ci_rerun_attempted"
                ):
                    # Worth exactly one automatic rerun before treating it
                    # as a failure - an abnormal queue delay can cancel
                    # jobs with no code-quality signal at all.
                    story["ci_rerun_attempted"] = True
                    _ci_rerun(pushed_sha)
                    ci = _merge_gate_ci_status(poll_branch, sha=pushed_sha)
                if ci["state"] == "fail":
                    gate_error = f"ci fail: {ci['error']}"
                    # Only a genuine test-failure verdict is "definitive" -
                    # cancelled (queue/infra flake, already given one
                    # auto-rerun above) and pending are NOT, and must keep
                    # retrying via the ordinary merge_attempts path below,
                    # not consume rework budget.
                    ci_definitive_fail = True
                elif ci["state"] == "cancelled":
                    gate_error = f"ci fail: {ci['error']}"
                elif ci["state"] == "pending":
                    story.setdefault("ci_pending_since", datetime.now(timezone.utc).isoformat())
                    story["ci_pending_sha"] = pushed_sha
                    if _ci_pending_expired(story["ci_pending_since"]):
                        _notify_user(
                            plan_name,
                            f"{key} CI has been pending since "
                            f"{story['ci_pending_since']} and exceeded the "
                            f"merge-gate pending bound; giving up on the wait.",
                            story_key=key,
                            severity="warning",
                            event="ci_pending_stalled",
                            dedup_key=f"ci_pending_stalled:{key}",
                            **(
                                {"correlation_id": story["correlation_id"]}
                                if story.get("correlation_id")
                                else {}
                            ),
                        )
                        story.pop("ci_pending_since", None)
                        story.pop("ci_pending_sha", None)
                        gate_error = f"ci pending: {ci['error']}"
                    else:
                        ci_wait = True
                if ci["state"] != "pending":
                    story.pop("ci_pending_since", None)
                    story.pop("ci_pending_sha", None)
            if ci_wait:
                summary["ci_pending"].append(key)
                continue
            if not gate_error:
                # Independent of review: re-run the acceptance oracle
                # against the just-rebased branch right before merging.
                # Closes the gap CI alone can't (a repo without CI, or a
                # CI-independent slip between tests_passed and review).
                acc = _reverify_acceptance(story, worktree, key)
                if acc["state"] == "fail":
                    gate_error = f"acceptance reverify fail: {acc['error']}"
            if not gate_error:
                # Independent of tests: a green suite doesn't mean the
                # project actually builds (PR #48 merged with `npm run
                # build` broken - retro §3.1).
                build = _reverify_build(worktree)
                if build["state"] == "fail":
                    gate_error = f"build reverify fail: {build['error']}"

            if gate_error:
                # Opt-in (PIPELINE_REWORK_ON_CI_FAIL=1): a DEFINITIVE CI test
                # failure - not a transient rebase/push error, not
                # pending/cancelled - can be caused by the agent's own
                # committed test file rather than the reviewed implementation
                # (the reviewer is acceptance-scoped and never saw it). Retrying
                # an unchanged branch identically MERGE_MAX_ATTEMPTS times can
                # never fix that; hand the CI failure back to the implementer as
                # rework feedback instead, bounded by the SAME rework budget
                # review_story uses, so a story that never converges still
                # parks/escalates rather than looping forever. See
                # MERGE_CI_REWORK_PLAN.md, 2026-07-17 (gpt-oss retry_backoff /
                # token_bucket: ground-truth-correct code abandoned because the
                # agent's own broken self-test tripped this gate).
                rework_ok = (
                    ci_definitive_fail
                    and os.environ.get("PIPELINE_REWORK_ON_CI_FAIL", "0") == "1"
                )
                if rework_ok:
                    # Bound by MERGE_MAX_ATTEMPTS via the merge_attempts
                    # counter, which PERSISTS across the rework -> review
                    # APPROVE -> merge-gate cycle. rework_attempts does NOT:
                    # the review APPROVE path pops it on every pass (the
                    # reviewer APPROVEs because it is acceptance-scoped and
                    # the oracle is green), so reusing rework_attempts here
                    # loops forever - each CI-fail re-increments 0->1 and the
                    # cap never exhausts (verified 2026-07-17 on token_bucket:
                    # four identical "routed to rework (1/3)" notifications,
                    # same broken assertion every round). merge_attempts is
                    # the merge gate's own counter and is not reset by review,
                    # so it bounds the loop: MERGE_MAX_ATTEMPTS rework rounds,
                    # then the fall-through below terminal-fails.
                    rework_ok = story.get("merge_attempts", 0) < MERGE_MAX_ATTEMPTS

                if rework_ok:
                    attempts = story.get("merge_attempts", 0) + 1
                    story["merge_attempts"] = attempts
                    # L1 (REVIEWER_ESCALATION_PLAN.md): flag this rework as
                    # CI-triggered so the next dispatch_story raises the
                    # agent's done-bar to full-suite-green (env
                    # LOCAL_AGENT_REWORK_FULL_SUITE). Without it the rework
                    # keeps the oracle-green bar and re-fails CI on the same
                    # assertion every round (the agent's own broken test is
                    # invisible to the acceptance-scoped oracle/reviewer).
                    story["ci_rework"] = True
                    story["review_feedback"] = _ci_rework_feedback(gate_error, attempts)
                    story["status"] = "changes_requested"
                    _notify_user(
                        plan_name,
                        f"{key} merge-gate CI failed ({gate_error}); "
                        f"routed to rework ({attempts}/{MERGE_MAX_ATTEMPTS}).",
                        event="merge_ci_rework",
                        **(
                            {
                                "correlation_id": story["correlation_id"],
                                "attempt": story.get("dispatch_attempts", 0),
                            }
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["notify"].append(key)
                    continue

                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = gate_error
                    _notify_user(
                        plan_name,
                        f"{key} merge gate failed {attempts}x "
                        f"({gate_error}); giving up - needs human intervention.",
                        event="merge_gate_failed",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(
                        plan_name,
                        f"{key} merge gate attempt {attempts}/"
                        f"{MERGE_MAX_ATTEMPTS} failed ({gate_error}); will retry.",
                        event="merge_gate_retry",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                summary["notify"].append(key)
                continue

            # _merge_pr removes the worktree and deletes the branch, so the
            # self-source diff must be taken BEFORE the merge, not after.
            mcp_touched = _mcp_self_source_touched(
                worktree, f"origin/{_default_branch()}"
            )
            try:
                _merge_pr(story.get("worktree", ""), key)
            except Exception as e:  # noqa: BLE001 (gh/git transient failure - see MERGE_MAX_ATTEMPTS)
                attempts = story.get("merge_attempts", 0) + 1
                story["merge_attempts"] = attempts
                if attempts >= MERGE_MAX_ATTEMPTS:
                    story["status"] = "failed"
                    story["merge_error"] = str(e)
                    _notify_user(
                        plan_name,
                        f"{key} merge failed {attempts}x "
                        f"({e}); giving up - needs human intervention.",
                        event="merge_failed",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                    summary["failed"].append(key)
                else:
                    # leave pr_open; the next tick retries within budget.
                    _notify_user(
                        plan_name,
                        f"{key} merge attempt {attempts}/"
                        f"{MERGE_MAX_ATTEMPTS} failed ({e}); will retry.",
                        event="merge_retry",
                        **(
                            {"correlation_id": story["correlation_id"]}
                            if story.get("correlation_id")
                            else {}
                        ),
                    )
                summary["notify"].append(key)
                continue
            story["status"] = "done"
            story.pop("merge_attempts", None)
            story.pop("parked_reason", None)
            story.pop("ci_rerun_attempted", None)
            story.pop("ci_rework", None)  # L1: clear the rework flag on done
            _mark_plane_done(key, plan_name)
            _notify_user(
                plan_name,
                f"{key} merged",
                story_key=key,
                event="story_merged",
                **(
                    {"correlation_id": story["correlation_id"]}
                    if story.get("correlation_id")
                    else {}
                ),
            )
            # A fully-done self-repo plan must enter the retro backlog no
            # matter which path marked the last story done (dedup inside
            # _record_retro_pending makes repeat calls across ticks safe).
            _maybe_record_retro(plan_name, manifest)
            if mcp_touched:
                _notify_user(plan_name, _mcp_restart_notice(mcp_touched))
                summary["notify"].append(key)
            summary["merged"].append(key)
        _atomic_write_json(manifest_path, manifest)

    return {"ok": True, **summary}
