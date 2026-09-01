"""TDD spec: the direct-write fallback must rotate the free-text .log too.

``pipeline.persistence._notify_user`` normally publishes on the wired bus and
``file_log_sink`` persists both artifacts (that path already rotates -- see
``test_w4l_notifications_rotation.py``).  But when the bus is unwired (or a
custom bus has no ``notification`` handlers), ``_notify_user`` falls back to
its internal ``_write_directly()``, which appends to the same
``<plan>.notifications.log``.  That third writer must route through the same
shared ``_rotate_if_needed`` helper so ``PIPELINE_NOTIFICATIONS_MAX_BYTES`` is
honored there too -- otherwise an unwired-bus deployment can still grow the
free-text log without bound.

These tests are expected to FAIL until the direct-write path rotates: the base
``.log`` reaches ~180 bytes with zero generation files.
"""

import pytest

from pipeline import persistence

PLAN = "dwplan"
LOG = f"{PLAN}.notifications.log"


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """Point persistence.PLAN_DIR (the Option B seam) at a tmp dir."""
    monkeypatch.setattr(persistence, "PLAN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def tiny_policy(monkeypatch):
    """Tiny policy via setattr on the module (NOT setenv + reload).

    The committed rotation tests patch the policy constants as module
    attributes because ``importlib.reload(persistence)`` would re-execute
    ``from .paths import PLAN_DIR`` and wipe the plan_dir fixture's PLAN_DIR
    patch (writes would land in the real PLAN_DIR). Module-attribute access
    inside the implementation resolves these per call, so setattr is both
    correct and reload-free.
    """
    monkeypatch.setattr(persistence, "NOTIFICATIONS_MAX_BYTES", 100)
    monkeypatch.setattr(persistence, "NOTIFICATIONS_KEEP_N", 2)

def _notify(plan: str, n: int) -> None:
    """Drive the direct-write fallback with a ~87-byte free-text line.

    The default wired bus never reaches ``_write_directly`` (get_bus always
    subscribes file_log_sink), so the fallback is reached by stubbing get_bus
    to raise: _notify_user's except branch then calls _write_directly().
    """
    from pipeline import event_wiring

    original = event_wiring.get_bus

    def boom():
        raise RuntimeError("E_NO_BUS simulated unwired bus")

    event_wiring.get_bus = boom
    try:
        persistence._notify_user(plan, f"m{n}-" + "x" * 50)
    finally:
        event_wiring.get_bus = original


def test_direct_write_rotates_free_text_log(plan_dir, tiny_policy):
    # Rotation is checked BEFORE each append (pinned by the committed
    # test_no_rotation_when_size_exactly_at_cap), so with ~87-byte lines and
    # cap=100 the base exceeds the cap at writes 3 and 5: two rotations,
    # producing .1 and .2.
    for n in range(5):
        _notify(PLAN, n)

    base = plan_dir / LOG
    gen1 = plan_dir / f"{LOG}.1"
    gen2 = plan_dir / f"{LOG}.2"

    assert gen1.exists(), (
        "the direct-write fallback must rotate the free-text .log: "
        f"{LOG}.1 missing after writes past the cap"
    )
    assert gen2.exists(), (
        "the second rotation must shift .1 -> .2 so no generation is lost"
    )
    assert base.stat().st_size <= 100, (
        f"the base .log must stay within the cap; got {base.stat().st_size} bytes"
    )
    # End state: .2 holds the oldest lines, .1 the middle, base the newest.
    assert "m0-" in gen2.read_text(encoding="utf-8")
    assert "m1-" in gen2.read_text(encoding="utf-8")
    assert "m2-" in gen1.read_text(encoding="utf-8")
    assert "m3-" in gen1.read_text(encoding="utf-8")
    assert "m4-" in base.read_text(encoding="utf-8")
