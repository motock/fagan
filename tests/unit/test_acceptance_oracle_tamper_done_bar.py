"""Acceptance oracle: a tampered acceptance fixture must also be refused at the
check_story_status done-bar, not only at the merge gate - otherwise a story
reaches tests_passed on a rewritten grader and only fails much later.
"""
import inspect

import pipeline.server as srv


def _source():
    # check_story_status is a plain module-level function, NOT an @mcp.tool();
    # look it up on the module, not in the tool registry.
    return inspect.getsource(srv.check_story_status)


def test_check_story_status_is_still_callable():
    assert callable(srv.check_story_status)


def test_the_done_bar_consults_the_tamper_detector():
    assert "_acceptance_tampered" in _source(), (
        "check_story_status must consult the tamper detector before granting "
        "tests_passed"
    )


def test_the_tamper_check_precedes_the_test_run():
    src = _source()
    tamper = src.index("_acceptance_tampered")
    scoped = src.index("_scope_test_cmd_to_acceptance")
    assert tamper < scoped, (
        "the tamper check must run before the scoped acceptance run, so a "
        "rewritten oracle is never executed as the done-bar"
    )


def test_the_operator_is_notified_on_refusal():
    src = _source()
    assert "_notify_user" in src[src.index("_acceptance_tampered"):], (
        "a refused done-bar must notify, not fail silently"
    )
