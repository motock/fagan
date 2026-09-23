"""PipelineService.request_decision's body, moved out of pipeline/service.py
(line-count target). The method keeps its signature and docstring and
delegates here. Server-owned names are _ServerRef bindings, exactly as in
pipeline.service, so tests patching pipeline.server still land.
"""

import logging
from datetime import datetime, timezone


def _request_decision_impl(self, plan_name: str, story_key: str, question: str, options: list[str], context: str=''):
    _validate_key(plan_name)
    _validate_key(story_key)
    with _scoped_repo_root(plan_name):
        policy = _load_policy()
    opts = "\n".join(f"  - {o}" for o in options)
    prompt = (
        f"A pipeline agent working on story {story_key} is blocked on a decision.\n\n"
        f"QUESTION: {question}\n\n"
        f"OPTIONS:\n{opts}\n\n"
        f"CONTEXT: {context}\n\n"
        f"DECISION POLICY:\n{policy}\n\n"
        f"Rule now, using your output contract exactly."
    )
    try:
        ruling = _parse_ruling(
            _invoke_overlord(prompt, plan_role_config=_plan_role_config(plan_name))
        )
    except Exception as exc:  # noqa: BLE001 - fail open: an overlord error must not reach the agent
        # Fail open: an overlord backend error must never surface to the
        # calling agent as a raw tool error. Park the story for a human and
        # return an actionable message instead (mirrors triage.rule_on_story).
        # Only the exception class name is recorded - never the traceback,
        # file paths or payload data (Secure-by-Design).
        summary = type(exc).__name__
        logging.getLogger("pipeline").warning(
            "request_decision overlord call failed open for story %s: %s",
            story_key,
            summary,
        )
        record = {
            "story_key": story_key,
            "question": question,
            "options": list(options),
            "ruling": "",
            "tier": "",
            "risk": "",
            "rationale": f"overlord call failed open: {summary}",
            "action": "park_for_human",
            "notify_user": True,
            "split": [],
            "failed_open": True,
            "summary": summary,
            "decided_by": "overlord",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _store.append_decision(plan_name, record)
        except Exception:  # noqa: BLE001 - persistence failure must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to append fail-open decision for story %s", story_key
            )
        try:
            _store.update_story(
                plan_name,
                story_key,
                {
                    "status": "parked",
                    "parked_reason": f"overlord failure: {summary}",
                },
            )
        except Exception:  # noqa: BLE001 - an already-parked story must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to park story %s after overlord failure", story_key
            )
        try:
            # Make the park visible to the operator (mirrors triage._park).
            # Only the exception class name is included - no payload data.
            _notify_user(
                plan_name,
                f"request_decision failed open for {story_key}: {summary}",
                story_key=story_key,
                severity="warning",
                event="overlord_failure",
            )
        except Exception:  # noqa: BLE001 - notification failure must not crash the tool
            logging.getLogger("pipeline").warning(
                "Failed to notify user about fail-open decision for story %s", story_key
            )
        return "decision escalated to human: story parked, see decisions log"
    record = {
        "story_key": story_key,
        "question": question,
        "options": list(options),
        **ruling,
        "decided_by": "overlord",
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    _store.append_decision(plan_name, record)
    return record


from .service import _ServerRef  # noqa: I001 - must follow the def: service.py back-imports _request_decision_impl (line 163), so defining it first keeps both import directions cycle-free

_invoke_overlord = _ServerRef("_invoke_overlord")
_load_policy = _ServerRef("_load_policy")
_notify_user = _ServerRef("_notify_user")
_parse_ruling = _ServerRef("_parse_ruling")
_plan_role_config = _ServerRef("_plan_role_config")
_scoped_repo_root = _ServerRef("_scoped_repo_root")
_store = _ServerRef("_store")
_validate_key = _ServerRef("_validate_key")
