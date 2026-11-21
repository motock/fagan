"""Carry out the park option a person chose, and resume the story.

``choose_park_option`` is the human half of :mod:`pipeline.park_options`: the
overlord prepared the options, a person picks one. The pick is a human
decision, so the authority matrix does not apply here; the caller authorizes
``decision:answer`` before calling.

Refusals are returned, not raised, as ``{"ok": False, "error": <code>}`` with
one of ``not_parked``, ``no_options``, ``options_stale`` or ``unknown_option``.
A refusal changes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import bound_records, triage_outcomes
from .actor import actor_id
from .park_options import _digest
from .triage import execute_ruling
from .unpark import UNPARKABLE_FIELDS, unpark_story

__all__ = ["choose_park_option", "latest_options_record"]


def latest_options_record(plan_name: str, story_key: str) -> dict | None:
    """The newest ``park_options`` decision recorded for the story, if any."""
    from .server import _store

    latest = None
    for record in _store.get_decisions(plan_name):
        if isinstance(record, dict) and record.get("kind") == "park_options" and record.get("story_key") == story_key:
            latest = record
    return latest


def _refuse(code: str) -> dict:
    return {"ok": False, "error": code}


def choose_park_option(plan_name: str, story_key: str, option_id: str) -> dict:
    from .server import _store

    manifest = bound_records.get_manifest(_store, plan_name)
    story = ((manifest or {}).get("stories") or {}).get(story_key)
    if not isinstance(story, dict) or story.get("status") != "parked":
        return _refuse("not_parked")
    record = latest_options_record(plan_name, story_key)
    if record is None:
        return _refuse("no_options")
    if record.get("parked_reason_digest") != _digest(story.get("parked_reason")):
        return _refuse("options_stale")
    option = next((o for o in record.get("options") or [] if o.get("id") == option_id), None)
    if option is None:
        return _refuse("unknown_option")

    action = option["action"]
    recommended = record.get("recommended")
    actor = actor_id()
    if action == "retry":
        result = unpark_story(plan_name, story_key, sorted(UNPARKABLE_FIELDS), status="interrupted", human_action="retry")
        new_status = result["new_status"]
    else:
        ruling = {
            "action": action,
            "park_class": record.get("park_class"),
            "rationale": option.get("rationale", ""),
            "diagnosis": option.get("diagnosis"),
            "decided_by": "human",
        }
        with _store.transaction(plan_name) as acquired:
            if not acquired:
                raise RuntimeError(f"could not acquire the plan lock for {plan_name!r}")
            manifest = bound_records.get_manifest(_store, plan_name)
            story = manifest["stories"][story_key]
            execute_ruling(plan_name, story_key, story, ruling, manifest)
            for s in manifest["stories"].values():
                if isinstance(s, dict):
                    s.pop("_patch_acceptance_recorded", None)
            _store.records().put_manifest("default", plan_name, manifest)
            new_status = story.get("status")
    triage_outcomes.record_human_resolution(
        plan_name,
        story_key,
        human_action=action if action in triage_outcomes.HUMAN_ACTIONS else "other",
        new_status=new_status,
        principal=actor,
        note=f"park option {option_id}; recommended {recommended}",
    )
    _store.append_decision(
        plan_name,
        {
            "kind": "park_option_chosen",
            "story_key": story_key,
            "option_id": option_id,
            "action": action,
            "recommended": recommended,
            "chose_recommended": option_id == recommended,
            "actor": actor,
            "decided_at": datetime.now(UTC).isoformat(),
        },
    )
    return {"ok": True, "story_key": story_key, "option_id": option_id, "action": action, "new_status": new_status}
