"""Namespace-aware behaviour of the advance queue (ADR 0011, ADR 0012).

Entries are keyed by ``(namespace, plan_name)``. ``None`` is the process
default namespace and is never merged with a named one. Namespaces take turns
in a rotation so one busy namespace cannot starve the others.
"""

from __future__ import annotations

import pytest

# pipeline.server must be imported before any other pipeline.* module: the
# package has a pre-existing import cycle, so a cold import of a leaf module
# first fails. Same idiom as tests/unit/test_advance_queue.py.
import pipeline.server  # noqa: F401
from pipeline.advance_queue import AdvanceQueue


def _drain_entries(queue: AdvanceQueue) -> list[tuple[str | None, str]]:
    """Take every queued entry, stopping when the queue reports empty."""
    taken: list[tuple[str | None, str]] = []
    while True:
        entry = queue.take_entry(0.05)
        if entry is None:
            return taken
        taken.append(entry)


# ---------------------------------------------------------------------------
# Keying and coalescing
# ---------------------------------------------------------------------------


def test_same_plan_name_in_two_namespaces_queues_twice():
    queue = AdvanceQueue()

    assert queue.request("apply", namespace="alpha-ns") is True
    assert queue.request("apply", namespace="beta-ns") is True

    assert _drain_entries(queue) == [("alpha-ns", "apply"), ("beta-ns", "apply")]


def test_same_plan_name_coalesces_independently_per_namespace():
    queue = AdvanceQueue()

    assert queue.request("apply", namespace="alpha-ns") is True
    assert queue.request("apply", namespace="alpha-ns") is False
    assert queue.request("apply", namespace="beta-ns") is True
    assert queue.request("apply", namespace="beta-ns") is False

    assert _drain_entries(queue) == [("alpha-ns", "apply"), ("beta-ns", "apply")]


def test_none_and_named_namespace_with_same_plan_are_distinct_entries():
    queue = AdvanceQueue()

    assert queue.request("apply") is True
    assert queue.request("apply", namespace="alpha-ns") is True

    assert _drain_entries(queue) == [(None, "apply"), ("alpha-ns", "apply")]


def test_request_while_in_flight_in_other_namespace_is_not_coalesced():
    queue = AdvanceQueue()

    assert queue.request("apply", namespace="alpha-ns") is True
    assert queue.take_entry(1.0) == ("alpha-ns", "apply")

    # The in-flight pair is (alpha-ns, apply); (beta-ns, apply) is a new entry.
    assert queue.request("apply", namespace="beta-ns") is True
    assert queue.take_entry(1.0) == ("beta-ns", "apply")


# ---------------------------------------------------------------------------
# Rotation between namespaces
# ---------------------------------------------------------------------------


def test_rotation_alternates_namespaces_in_take_order():
    queue = AdvanceQueue()

    assert queue.request("a1", namespace="alpha-ns") is True
    assert queue.request("a2", namespace="alpha-ns") is True
    assert queue.request("a3", namespace="alpha-ns") is True
    assert queue.request("b1", namespace="beta-ns") is True

    assert _drain_entries(queue) == [
        ("alpha-ns", "a1"),
        ("beta-ns", "b1"),
        ("alpha-ns", "a2"),
        ("alpha-ns", "a3"),
    ]


def test_fifo_order_holds_within_one_namespace():
    queue = AdvanceQueue()

    assert queue.request("p1", namespace="alpha-ns") is True
    assert queue.request("p2", namespace="alpha-ns") is True
    assert queue.request("p3", namespace="alpha-ns") is True

    assert _drain_entries(queue) == [
        ("alpha-ns", "p1"),
        ("alpha-ns", "p2"),
        ("alpha-ns", "p3"),
    ]


def test_dirty_plan_requeues_at_tail_of_its_own_namespace():
    queue = AdvanceQueue()

    assert queue.request("p1", namespace="alpha-ns") is True
    assert queue.request("p2", namespace="alpha-ns") is True
    assert queue.request("q1", namespace="beta-ns") is True

    assert queue.take_entry(1.0) == ("alpha-ns", "p1")
    # Dirty mark for the in-flight pair; re-queued when it finishes.
    assert queue.request("p1", namespace="alpha-ns") is False
    queue.finish("p1", namespace="alpha-ns")

    # Rotation is now beta, alpha: the re-queued p1 lands behind p2.
    assert _drain_entries(queue) == [
        ("beta-ns", "q1"),
        ("alpha-ns", "p2"),
        ("alpha-ns", "p1"),
    ]


# ---------------------------------------------------------------------------
# finish / last_advanced are per pair
# ---------------------------------------------------------------------------


def test_finishing_one_pair_does_not_stamp_the_same_plan_in_another_namespace():
    queue = AdvanceQueue()

    assert queue.request("apply", namespace="alpha-ns") is True
    assert queue.request("apply", namespace="beta-ns") is True
    assert queue.take_entry(1.0) == ("alpha-ns", "apply")
    assert queue.take_entry(1.0) == ("beta-ns", "apply")

    queue.finish("apply", namespace="alpha-ns")

    assert queue.last_advanced("apply", namespace="alpha-ns") is not None
    assert queue.last_advanced("apply", namespace="beta-ns") is None


def test_last_advanced_is_separate_for_none_and_named_namespace():
    queue = AdvanceQueue()

    assert queue.request("apply") is True
    assert queue.take_entry(1.0) == (None, "apply")
    queue.finish("apply")

    assert queue.last_advanced("apply") is not None
    assert queue.last_advanced("apply", namespace="alpha-ns") is None


# ---------------------------------------------------------------------------
# Namespace validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("namespace", ["", "A/B/C", "Up", 5])
def test_invalid_namespace_is_rejected_without_echoing_the_value(namespace):
    queue = AdvanceQueue()

    with pytest.raises(ValueError) as excinfo:
        queue.request("apply", namespace=namespace)

    assert str(excinfo.value) == "namespace is not valid"
    assert "Up" not in str(excinfo.value)
    assert _drain_entries(queue) == []


# ---------------------------------------------------------------------------
# take() compatibility and the namespaced guard
# ---------------------------------------------------------------------------


def test_take_returns_plain_plan_name_for_none_namespace_entry():
    queue = AdvanceQueue()

    assert queue.request("apply") is True

    assert queue.take(1.0) == "apply"


def test_take_raises_for_named_namespace_entry():
    queue = AdvanceQueue()

    assert queue.request("apply", namespace="alpha-ns") is True

    with pytest.raises(RuntimeError, match="namespaced entry needs take_entry"):
        queue.take(1.0)


# ---------------------------------------------------------------------------
# Closed queue
# ---------------------------------------------------------------------------


def test_closed_queue_rejects_request_for_any_namespace():
    queue = AdvanceQueue()
    queue.close()

    assert queue.request("apply") is False
    assert queue.request("apply", namespace="alpha-ns") is False
    assert queue.take_entry(0.05) is None
