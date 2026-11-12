"""The triage ruling executors no longer take ``manifest_path``.

Every executor persists through the record store, so the file path is a
vestigial argument threaded through ``execute_ruling`` and
``_apply_ruling_for_mode``. ADR 0012 stage C removes ``manifest_path`` from the
Store Protocol; these signatures must not depend on it.
"""

import inspect

import pytest

from pipeline import (
    triage,
    triage_patch_acceptance,
    triage_repo_issue,
    triage_scope_actions,
    triage_story_actions,
)

_RULING_PARAMS = ["plan_name", "story_key", "story", "ruling", "manifest"]

_RULING_ENTRYPOINTS = [
    (triage, "execute_ruling"),
    (triage, "_apply_ruling_for_mode"),
]

_EXECUTORS = [
    (triage_story_actions, "_execute_split_story"),
    (triage_story_actions, "_execute_mark_done"),
    (triage_patch_acceptance, "_execute_patch_acceptance"),
    (triage_repo_issue, "_execute_repo_issue"),
    (triage_scope_actions, "_execute_amend_scope"),
]


@pytest.mark.parametrize("module,name", _RULING_ENTRYPOINTS + _EXECUTORS)
def test_callable_takes_no_manifest_path_parameter(module, name):
    params = list(inspect.signature(getattr(module, name)).parameters)

    assert params == _RULING_PARAMS, (
        f"{name} must take exactly {_RULING_PARAMS}, got {params}"
    )


@pytest.mark.parametrize("module,name", _RULING_ENTRYPOINTS + _EXECUTORS)
def test_passing_the_old_manifest_path_argument_is_a_type_error(module, name):
    # Bind the six positional arguments the old signature accepted. Binding
    # uses the same rules Python applies at call time, so this raises the
    # TypeError a caller passing the removed argument would see, with no
    # executor body run.
    sig = inspect.signature(getattr(module, name))

    with pytest.raises(TypeError):
        sig.bind("plan", "KEY", {}, {}, {"stories": {}}, "plan.manifest.json")
