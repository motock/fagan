"""Tests for the request namespace seam (``pipeline.actor``).

The namespace sits beside the bound principal and the correlation id: it says
*which namespace* a request operates in. These tests pin the default, the
binding and restore semantics (including on exception and under nesting),
isolation across threads and asyncio tasks, the refusal of bad values, and
that binding a namespace leaves the principal and correlation seams unchanged.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.identity import Principal
from pipeline.actor import (
    actor_id,
    bind_correlation_id,
    bind_namespace,
    bind_principal,
    current_correlation_id,
    current_namespace,
    current_principal,
)
from pipeline.store_records import DEFAULT_NAMESPACE


def test_unbound_namespace_is_the_default_namespace() -> None:
    """Outside any binding the request operates in the default namespace."""
    assert current_namespace() == DEFAULT_NAMESPACE


def test_bound_namespace_is_visible_inside_the_block() -> None:
    """A transport's namespace is what every layer reads."""
    with bind_namespace("ns-a"):
        assert current_namespace() == "ns-a"


def test_namespace_binding_is_restored_after_the_block() -> None:
    """The binding must not leak past the request."""
    with bind_namespace("ns-a"):
        pass
    assert current_namespace() == DEFAULT_NAMESPACE


def test_namespace_binding_is_restored_after_an_exception() -> None:
    """An exception inside the request still restores the prior namespace."""
    with pytest.raises(RuntimeError), bind_namespace("ns-a"):
        raise RuntimeError("boom")
    assert current_namespace() == DEFAULT_NAMESPACE


def test_nested_bindings_restore_in_reverse_order() -> None:
    """Leaving an inner binding returns to the outer one, then to the default."""
    with bind_namespace("outer"):
        with bind_namespace("inner"):
            assert current_namespace() == "inner"
        assert current_namespace() == "outer"
    assert current_namespace() == DEFAULT_NAMESPACE


def test_namespace_is_isolated_across_threads() -> None:
    """A thread binding its own namespace must not change the caller's view."""
    seen: dict[str, str] = {}

    def worker() -> None:
        seen["unbound_thread"] = current_namespace()
        with bind_namespace("thread-b"):
            seen["bound_thread"] = current_namespace()

    with bind_namespace("main-a"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert current_namespace() == "main-a"

    assert seen["bound_thread"] == "thread-b"
    assert seen["unbound_thread"] == DEFAULT_NAMESPACE


def test_namespace_is_isolated_across_asyncio_tasks() -> None:
    """A task binding its own namespace must not change the awaiting caller."""

    async def child() -> str:
        with bind_namespace("task-b"):
            await asyncio.sleep(0)
            return current_namespace()

    async def parent() -> tuple[str, str]:
        with bind_namespace("parent-a"):
            child_view = await asyncio.create_task(child())
            return current_namespace(), child_view

    parent_view, child_view = asyncio.run(parent())

    assert parent_view == "parent-a"
    assert child_view == "task-b"


@pytest.mark.parametrize("bad_value", [None, "", 0, 42, b"ns-a", ["ns-a"]])
def test_bind_namespace_refuses_invalid_values(bad_value: object) -> None:
    """None, the empty string and non-str values are refused, not coerced."""
    with pytest.raises(ValueError), bind_namespace(bad_value):  # type: ignore[arg-type]
        pass


def test_refused_namespace_leaves_the_default_in_place() -> None:
    """A refused binding must never have set the namespace."""
    with pytest.raises(ValueError), bind_namespace(""):
        pass
    assert current_namespace() == DEFAULT_NAMESPACE


def test_binding_a_namespace_leaves_principal_and_correlation_unchanged() -> None:
    """The namespace is additive: the unchanged principal path still resolves.

    With no namespace bound the principal and correlation seams behave exactly
    as before this seam existed; binding a namespace must not perturb them.
    """
    principal = Principal(subject="alice", auth_method="session")

    with (
        bind_principal(principal),
        bind_correlation_id("req-1"),
        bind_namespace("ns-a"),
    ):
        assert current_principal() == principal
        assert actor_id() == "alice"
        assert current_correlation_id() == "req-1"
