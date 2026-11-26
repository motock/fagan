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

import logging
import os
import re
from typing import Any

from . import access_hooks, bound_records, cross_plan, plan_approval, review_job  # noqa: F401  (access_hooks re-exported)
from . import merge as _merge_mod  # noqa: F401  (read by advance_merge via _ModuleRef)
from .advance_failed_stories import poll_running_stories
from .advance_merge import (
    _MERGE_HOLD_REASON,  # noqa: F401  (re-exported for existing readers)
    _adjudicate_merges,
    _readjudicate_parked_merge_hold,  # noqa: F401  (re-exported for existing readers)
)
from .advance_ready_dispatch import dispatch_ready_stories
from .advance_review import review_or_collect
from .autonomy_settings import effective_autonomy
from .ci import context_from_manifest
from .concurrency import (
    PlanLockReacquireTimeout,  # noqa: F401  (re-exported for existing readers)
    _has_live_test_author_phase,
    _released_plan_lock,
)
from .config import PIPELINE_MAX_DISPATCH_PER_TICK as _CFG_MAX_DISPATCH_PER_TICK
from .dispatch import _resolve_dispatch_target
from .dispatch_fence import blocks_reclaim  # noqa: F401  (re-exported for existing readers)
from .dispatch_hold import dispatch_held, filter_ready, pause_reason
from .dispatch_lease import claim_dispatch_lease  # noqa: F401  (re-exported for existing readers)
from .park_options import run_park_options_sweep
from .rebrief import (
    collect_failure_evidence,  # noqa: F401  (re-exported for existing readers)
    compose_rebriefed_instructions,  # noqa: F401  (re-exported for existing readers)
    detect_unsatisfiable_signal,  # noqa: F401  (re-exported for existing readers)
    diagnose_failure,  # noqa: F401  (re-exported for existing readers)
)
from .review_gate_notice import clear_review_gate_notice, should_notify_review_gate
from .role_routing_scope import layer_routing_for_manifest, layer_routing_kwargs
from .stall_scan import run_stall_scan
from .store_records import validate_plan_name
from .wedge_io import run_wedge_scan

# MERGEATTR-1: ``_merge_mod``'s story-key adjudication context is bound around
# each merge-gate call so the high-risk adjudication record names the story it
# ruled on. Taken straight from pipeline.merge rather than via _ServerRef:
# pipeline.server's explicit `from .merge import (...)` re-export list cannot be
# extended by this two-file change, so a _ServerRef binding would AttributeError
# at tick time - the same reasoning the LOCKSTARVE-A3 note below records for
# PIPELINE_MAX_DISPATCH_PER_TICK. The context manager is pure ContextVar state
# owned by pipeline.merge (no server global is involved), so the direct module
# reference is the correct seam.


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

    def __str__(self):
        return str(self._value())

    def __repr__(self):
        return repr(self._value())


# Server-sourced members the moved functions reference as free variables. Each
# resolves to the live ``pipeline.server`` binding at call time so
# ``monkeypatch.setattr(pipeline.server, "NAME", ...)`` still lands.
DISPATCH_MAX_ATTEMPTS = _ServerRef("DISPATCH_MAX_ATTEMPTS")
MAX_CONCURRENT_AGENTS = _ServerRef("MAX_CONCURRENT_AGENTS")
# LOCKSTARVE-A3: read straight from pipeline.config rather than via _ServerRef.
# _ServerRef delegates to pipeline.server, whose explicit `from .config import
# (...)` list cannot be extended by this story (exactly two production files),
# so a _ServerRef binding would AttributeError at tick time. A module-level
# assignment still lands for the tests' _set_cap, which monkeypatches this
# module attribute directly.
PIPELINE_MAX_DISPATCH_PER_TICK = _CFG_MAX_DISPATCH_PER_TICK
MERGE_MAX_ATTEMPTS = _ServerRef("MERGE_MAX_ATTEMPTS")
PIPELINE_AUTONOMY = _ServerRef("PIPELINE_AUTONOMY")
_atomic_write_json = _ServerRef("_atomic_write_json")
_auto_escalation_enabled = _ServerRef("_auto_escalation_enabled")
_ci_pending_expired = _ServerRef("_ci_pending_expired")
_ci_rerun = _ServerRef("_ci_rerun")
_ci_rework_feedback = _ServerRef("_ci_rework_feedback")
_completed_dep_ids = _ServerRef("_completed_dep_ids")
_count_in_progress_agents = _ServerRef("_count_in_progress_agents")
_default_branch = _ServerRef("_default_branch")
_escalate_to_claude = _ServerRef("_escalate_to_claude")
_escalate_to_local_fallback_model = _ServerRef("_escalate_to_local_fallback_model")
_escalation_label = _ServerRef("_escalation_label")
_escalation_target = _ServerRef("_escalation_target")
_LOCAL_BACKEND_NAMES = _ServerRef("_LOCAL_BACKEND_NAMES")
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
# REG-1: the per-story dispatch gate resolves through _resolve_dispatch_target
# (imported from pipeline.dispatch below), not through _resolve_dispatch_backend.
# The name is still bound here because tests/unit/test_notification_event_names.py
# monkeypatches advance._resolve_dispatch_backend; the gate no longer calls it, so
# that stub is inert - its owner must update it (see the PR reply).
_resolve_dispatch_backend = _ServerRef("_resolve_dispatch_backend")
_reverify_acceptance = _ServerRef("_reverify_acceptance")
_reverify_build = _ServerRef("_reverify_build")
_role_resource_ok = _ServerRef("_role_resource_ok")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_store = _ServerRef("_store")
merge_adjudication_plan = _ServerRef("merge_adjudication_plan")
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
    """Run the advisory pre-tick sweeps (triage, wedge, stall), then the tick
    proper. Rename-and-delegate: the body moved verbatim to
    _advance_pipeline_locked_impl; every sweep is advisory and fail-open (C2).
    """
    try:
        run_triage_sweep(plan_name)
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning("triage sweep raised; tick continues")
    run_park_options_sweep(plan_name)  # never raises (see pipeline/park_options.py)
    try:
        run_wedge_scan(plan_name)
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning("wedge scan raised; tick continues")
    try:
        run_stall_scan(plan_name)
    except Exception:  # noqa: BLE001 (fail-open by design; the tick must survive it)
        logging.getLogger("pipeline").warning("stall scan raised; tick continues")
    return _impl_ref(plan_name)


# "dispatch is waiting" notice (hrf-04): a tick whose dispatch-eligible stories
# were held back by the dispatch gate says why, so a dead backend is not silent.
# A gate reason can embed a URL and can span lines, so it is sanitised first.
_GATE_REASON_MAX_CHARS = 200
_GATE_REASON_FALLBACK = "dispatch backend unavailable"
_GATE_URL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)(\S*)")
_GATE_WHITESPACE_RE = re.compile(r"\s+")
# Reasons already announced per plan in the gated episode in progress.
# _notify_user records a dedup_key but does not suppress on it, so the
# once-per-episode guard lives here, in-process.
_GATE_ANNOUNCED: dict[str, set[str]] = {}


def _strip_url_credentials_and_query(match: re.Match[str]) -> str:
    """Keep ``scheme://host/path``; drop userinfo, query and fragment."""
    scheme, rest = match.group(1), match.group(2)
    without_query = re.split(r"[?#]", rest, maxsplit=1)[0]
    host, slash, path = without_query.partition("/")
    host = host.rpartition("@")[2]
    return f"{scheme}{host}{slash}{path}"


def sanitise_gate_reason(reason: str | None) -> str:
    """One line, at most ``_GATE_REASON_MAX_CHARS``, no URL credentials or query."""
    if not reason:
        return ""
    one_line = _GATE_WHITESPACE_RE.sub(" ", str(reason)).strip()
    clean = _GATE_URL_RE.sub(_strip_url_credentials_and_query, one_line)
    return clean[:_GATE_REASON_MAX_CHARS].rstrip()


def _group_gated_stories(gated: list[tuple[str, str | None]]) -> dict[str, list[str]]:
    """Group ``(story_key, raw_reason)`` pairs by their sanitised reason.

    Insertion order is kept, so the first gated story sets the first group.
    A story whose gate gave no reason is grouped under a fixed fallback.
    """
    groups: dict[str, list[str]] = {}
    for key, raw_reason in gated:
        reason = sanitise_gate_reason(raw_reason) or _GATE_REASON_FALLBACK
        groups.setdefault(reason, []).append(key)
    return groups


def _take_new_gate_reasons(plan_name: str, reasons: list[str]) -> list[str]:
    """Return the reasons not yet announced for this plan in this episode.

    ``reasons`` is the full set gated this tick. A reason that is no longer
    gated is forgotten, so the gate going down again is announced again. An
    empty ``reasons`` (gate up, or nothing eligible) ends the episode.
    """
    announced = _GATE_ANNOUNCED.get(plan_name, set())
    fresh = [reason for reason in reasons if reason not in announced]
    if reasons:
        _GATE_ANNOUNCED[plan_name] = set(reasons)
    else:
        _GATE_ANNOUNCED.pop(plan_name, None)
    return fresh


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

    Falls back to ``dispatched_model`` (the concrete tag dispatch_story
    resolved and actually launched) when the plan-authored ``model`` field
    is unset — a story dispatched with no explicit ``model`` override still
    lands on a concrete backend/tag, and that tag is what determines its
    real footprint. Reading only ``model`` treated every such story as
    on-device even when it was actually dispatched to a ``:cloud`` model.
    """
    tag = story.get("model") or story.get("dispatched_model")
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
    for manifest in bound_records.get_manifests(_store).values():
        for story in manifest.get("stories", {}).values():
            if _has_live_test_author_phase(story):
                if _story_dispatch_is_on_device(story):
                    count += 1
                continue
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


# (ready story, neighbour plan, neighbour key) triples already announced by this
# process, so a story that stays ready is not re-notified every tick.
_NOTIFIED_FILE_OVERLAPS: set[tuple[str, str, str, str]] = set()


def _warn_file_overlaps(plan_name: str, stories: dict, ready: list[str], neighbours: dict) -> None:
    """Advise (never block) once per process per (plan, story, neighbour) pair."""
    others = cross_plan.in_flight_stories(neighbours)
    for key in ready:
        for hit in cross_plan.file_overlaps(stories[key].get("files"), others):
            pair = (plan_name, key, hit["plan"], hit["key"])
            if pair in _NOTIFIED_FILE_OVERLAPS:
                continue
            _NOTIFIED_FILE_OVERLAPS.add(pair)
            shared = sorted(set(stories[key]["files"]) & set(hit["files"]))
            _notify_user(
                plan_name,
                f"{key}: shares {', '.join(shared)} with in-flight "
                f"{hit['plan']}/{hit['key']}; consider sequencing",
                story_key=key,
                event="file_overlap",
            )


def _fresh_manifest(plan_name: str) -> dict[str, Any]:
    """Fresh store read (never a get_manifests snapshot); raise if it is gone."""
    manifest = bound_records.get_manifest(_store, plan_name)
    if manifest is None:
        raise FileNotFoundError("manifest not found")
    return manifest


def _advance_pipeline_locked_impl(plan_name: str) -> dict[str, Any]:
    validate_plan_name(plan_name)
    if plan_name not in bound_records.list_plans(_store):
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

    # ENT-LC-04: an unapproved plan must not dispatch, review or merge. This
    # sits before the resource gates so an unapproved plan never even
    # adjudicates merges, and it re-reads the record on every tick (no cached
    # negative) so approving the plan takes effect on the next tick.
    if not plan_approval.is_activatable(manifest):
        return {"ok": True, "skipped": "plan_not_approved", **plan_approval.outdated_reason(manifest)}

    # Per-backend resource gate (Step 5): dispatch and review can run on
    # different backends, so gate each by ITS backend's availability rather
    # than one global Claude flag. This is what lets local dispatch keep
    # running when Claude's weekly limit is hit (and vice versa).
    dispatch_ok, _dispatch_reason = _role_resource_ok("dispatch")
    review_ok, review_reason = _role_resource_ok(
        "review",
        plan_role_config=manifest.get("role_config"),
        **layer_routing_kwargs(layer_routing_for_manifest(manifest)),
    )

    done = _completed_dep_ids(stories)
    neighbours = {
        name: other
        for name, other in bound_records.get_manifests(_store).items()
        if name != plan_name
    }
    ready = [
        k
        for k, v in stories.items()
        if v["status"] in ("todo", "interrupted", "changes_requested")
        and all(d in done for d in v.get("dependencies", []))
        and not cross_plan.blocked_by_gaps(v, neighbours)
    ]

    # The deployment hold stops new dispatch only: review and merge still run,
    # and dispatch_ok is untouched so an in-flight agent is never interrupted.
    held = dispatch_held(_store)
    ready = filter_ready(ready, held)

    _warn_file_overlaps(plan_name, stories, ready, neighbours)

    # Dereference once: PIPELINE_AUTONOMY is a _ServerRef proxy (comparisons
    # like `== "dry-run"` work via its __eq__), but the raw proxy object is
    # not JSON-serializable, so anything embedded in the returned dict must
    # use the resolved plain string instead.
    autonomy = effective_autonomy(PIPELINE_AUTONOMY._value(), context_from_manifest(manifest))

    if autonomy == "dry-run":
        return {
            "ok": True,
            "dry_run": True,
            "autonomy": autonomy,
            # "paused" kept for back-compat = dispatch gated.
            "paused": not dispatch_ok or held,
            "dispatch_paused": not dispatch_ok or held,
            **pause_reason(held),
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
        "paused": not dispatch_ok or held,
        "dispatch_paused": not dispatch_ok or held,
        **pause_reason(held),
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
        _adjudicate_merges(plan_name, summary)
        # Per-story dispatch gate. The dispatch backend is resolved per-story
        # (dispatch_story's own resolution, shared via _resolve_dispatch_target),
        # so the gate must be per-story too: a :cloud-tagged model (served via
        # Ollama with zero local VRAM footprint) or a Claude-routed story must
        # never be blocked by the LOCAL free-memory floor, while an on-device
        # model keeps the floor exactly as before. Reachability still applies
        # to every backend (a cloud model proxied through an unreachable server
        # cannot dispatch).
        # Per-story dispatch resolution (REG-1): the gate resolves each
        # story's (provider, model) through the SAME
        # _resolve_dispatch_target seam dispatch_story uses, so the gate can
        # never gate on one model while dispatch runs another. The dispatch
        # env var's own priority (plan role_config -> env -> registry ->
        # "claude") is applied inside resolve_role, so the env is still
        # honoured without a raw read here.
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
            story_backend, _story_model = _resolve_dispatch_target(
                story,
                plan_role_config=manifest.get("role_config"),
                **layer_routing_kwargs(layer_routing_for_manifest(manifest)),
            )
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
            # Falls back to dispatched_model: a story with no plan-authored
            # `model` override still resolves to a concrete tag at dispatch
            # time, and that's the tag that determines its real footprint
            # (see _story_dispatch_is_on_device).
            tag = story.get("model") or _story_model or story.get("dispatched_model")
            if tag and tag.endswith(":cloud"):
                continue
            if tag:
                status = backend.get_backend("dispatch", name=story_backend).resource_status(
                    model_tag=tag
                )
                if not status.get("ok", True) and "insufficient free memory" not in (
                    status.get("reason") or ""
                ):
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)
            else:
                if not dispatch_ok and not memory_pressure:
                    interrupt_story(plan_name, key)
                    summary["interrupted"].append(key)

        # 1. Dispatch ready (and resumable-interrupted) stories: see
        # pipeline/advance_ready_dispatch.py. Runs under this tick's plan lock.
        dispatch_ready_stories(plan_name, manifest, stories, ready, summary)

        # 2. Poll running agents: tests fail -> notify (or escalate); tests pass
        # -> tests_passed. Polling is per-story, mirroring the interruption gate
        # above: a story this tick would interrupt (blanket gate down for a
        # non-memory reason, or its own memory floor not met) is NOT polled; a
        # surviving story IS polled even when the blanket dispatch gate is down.
        # This keeps a :cloud story dispatched under local memory pressure from
        # stalling in_progress unpolled — dispatch and interruption are already
        # per-story, so polling must be too. Review below stays gated by the
        # REVIEW backend (review_ok); merge (further below) is unconditional.
        manifest = _fresh_manifest(plan_name)
        stories = manifest["stories"]
        poll_running_stories(
            plan_name, manifest, stories, summary,
            dispatch_ok=dispatch_ok, memory_pressure=memory_pressure,
        )

        # Review every tests_passed story (incl. ones orphaned by a crashed
        # review on a prior tick - review_story is idempotent). Gated by the
        # REVIEW backend independently of dispatch: a Claude-dispatch pause no
        # longer blocks reviewing already-finished work on a healthy review
        # backend, and a local-dispatch run can still defer review if review
        # is on Claude and Claude is gated.
        if review_ok:
            clear_review_gate_notice(plan_name)
            stories = _fresh_manifest(plan_name)["stories"]
            # LOCKSTARVE-B4: release the plan lock around review_story, the
            # same synchronous model-call-heavy pattern LOCKSTARVE-B3 applied
            # to dispatch_story. No dispatch lease is needed here -
            # review_story (pipeline/review_orchestrator.py) re-reads the
            # manifest itself and is a documented no-op skip for any story
            # not in "tests_passed", so a second tick/process entering it
            # concurrently cannot corrupt the manifest, and cannot
            # sequentially double-review a story already advanced past
            # tests_passed. It does not rule out two calls racing the
            # SAME simultaneous check-pass window - review_story's own
            # guard bounds that outcome, not this loop.
            #
            # But the per-iteration STATUS CHECK must still come from a
            # FRESH on-disk read, never the snapshot taken before the loop -
            # an earlier iteration's own released-lock window can let
            # another actor already review and advance a later story in
            # this same snapshot. Checking the stale in-memory status would
            # call review_story on it again anyway: review_story's own guard
            # makes that safe from a data-corruption standpoint, but it
            # still emits a spurious "review skipped" notification and a
            # misleading summary["advanced"] entry claiming this tick
            # advanced a story it did not touch. Only the set of keys to
            # consider is safe to snapshot; the status of each must be
            # re-read per iteration.
            for key in list(stories.keys()):
                fresh_story = _fresh_manifest(plan_name)["stories"].get(key)
                if fresh_story is None:
                    continue
                # The detached path also sees a story an operator moved out of
                # tests_passed while its review job was live: that job must be
                # cancelled. review_or_collect starts a job only for a
                # tests_passed story, so passing a moved story on is safe.
                if review_job.detached_enabled() and (
                    fresh_story["status"] == "tests_passed"
                    or fresh_story.get("review_job")
                ):
                    review_or_collect(plan_name, key, fresh_story, summary)
                    continue
                if fresh_story["status"] != "tests_passed":
                    continue
                with _released_plan_lock(plan_name):
                    rv = review_story(plan_name, key)
                if rv.get("ok") is False or rv.get("skipped"):
                    # ok:False - a concurrent re-ingest dropped the story
                    # during the released window (review_story returns
                    # {"ok": False, "error": ...}, no "status" key).
                    # skipped - review_story's own plan-flock guard
                    # (_plan_lock) could not take the plan lock during the
                    # window this loop deliberately opened (the {"ok": True,
                    # "skipped": "locked"} shape, also no "status" key), so no
                    # review ran. Either way there is no status to report and
                    # rv["status"] would KeyError the whole tick; record the
                    # skip and let a later tick retry - the story is still
                    # tests_passed on disk. Same handling LOCKSTARVE-B3
                    # applies to dispatch_story's contention return.
                    summary.setdefault("skipped", []).append(key)
                    continue
                summary["advanced"].append({key: rv["status"]})
                if rv.get("deferred") == "rate_limited":
                    summary["review_deferred"].append(key)
        else:
            # Only surface the review gate when there is tests_passed work
            # waiting to be reviewed this tick; otherwise the notification is
            # noise (e.g. all stories are already pr_open awaiting merge).
            if any(s["status"] == "tests_passed" for s in stories.values()):
                if should_notify_review_gate(plan_name):
                    _notify_user(
                        plan_name,
                        f"Review backend gated ({review_reason}): deferring review.",
                        dedup_key=f"review_gate:{plan_name}:{review_reason}",
                    )
                summary["notify"].append("review_paused")

    return {"ok": True, **summary}
