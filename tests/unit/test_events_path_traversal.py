import pytest

from pipeline.events import JsonlEventBus

# Define unsafe plan identifiers that should be rejected.
_UNSAFE_PLANS = [
        "",          # empty string
    None,         # None value
    ".",         # current directory
    "..",        # parent directory
    "../evil",   # relative traversal up one level
    "a/b",       # contains path separator
    "..\\evil",  # Windows style separator
    "sub/../../escape"  # complex traversal
]

@pytest.mark.parametrize("plan", _UNSAFE_PLANS)
def test_publish_rejects_unsafe_plan(tmp_path, plan):
    bus = JsonlEventBus(root=tmp_path / "root")
    with pytest.raises(ValueError):
        # The publish method expects an event dict; the plan is inferred from the event key.
        # For these tests we use a dummy event that contains the plan under a reserved key.
        bus.publish(event={"plan": plan, "data": {}})

@pytest.mark.parametrize("plan", _UNSAFE_PLANS)
def test_drain_rejects_unsafe_plan(tmp_path, plan):
    bus = JsonlEventBus(root=tmp_path / "root")
    with pytest.raises(ValueError):
        bus.drain(plan=plan)
