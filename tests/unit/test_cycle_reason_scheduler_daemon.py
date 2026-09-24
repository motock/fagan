"""Guards the corrected import-cycle rationale in ``pipeline.scheduler_daemon``."""

import inspect

from pipeline import scheduler_daemon


def test_module_docstring_states_the_real_reason():
    src = inspect.getsource(scheduler_daemon)
    assert "create a circular import" not in src
    assert "pipeline.server imports from this module at import time" not in src
    assert "would not see the patch" in src
    assert "land on this call site" in src


def test_run_daemon_still_applies_the_clamp_before_the_lazy_import():
    src = inspect.getsource(scheduler_daemon)
    run_at = src.index("def run_daemon")
    clamp_at = src.index("_apply_scheduler_role_call_clamp()", run_at)
    import_at = src.index("from .server import advance_all_plans", run_at)
    assert clamp_at < import_at
