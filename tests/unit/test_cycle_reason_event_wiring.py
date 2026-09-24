"""Guards the corrected import-cycle rationale in ``pipeline.event_wiring``."""

import inspect

from pipeline import event_wiring


def test_wake_handler_comment_states_the_real_reason():
    src = inspect.getsource(event_wiring)
    assert "avoid circular dependency" not in src
    assert "land on this call site" in src


def test_wake_handler_still_imports_advance_pipeline_lazily():
    assert not hasattr(event_wiring, "advance_pipeline")
