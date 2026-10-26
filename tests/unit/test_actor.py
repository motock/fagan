"""Tests for the request-scoped actor seam (``pipeline.actor``).

The seam exists so that "who is asking" is established by the transport that
received a request, not by an argument to the tool the caller invokes. These
tests pin the binding semantics (bind, restore, nest, thread isolation) and
guard the design: no MCP tool may take an identity-shaped parameter.
"""

from __future__ import annotations

import inspect
import threading

from app.identity import LOCAL_OPERATOR, Principal
from pipeline.actor import ANONYMOUS, actor_id, bind_principal, current_principal


def test_unbound_resolves_to_local_operator() -> None:
    """With nothing bound, the process itself is the local operator."""
    assert (current_principal(), actor_id()) == (LOCAL_OPERATOR, "local-operator")


def test_bound_principal_is_visible_inside_the_block() -> None:
    """A principal bound by the transport is what every layer reads."""
    principal = Principal(subject="alice", auth_method="token")
    with bind_principal(principal):
        assert (current_principal(), actor_id()) == (principal, principal.subject)


def test_binding_is_restored_after_the_block() -> None:
    """The binding must not leak past the request (a module global would)."""
    with bind_principal(Principal(subject="alice", auth_method="token")):
        pass
    assert (current_principal(), actor_id()) == (LOCAL_OPERATOR, "local-operator")


def test_nested_binding_restores_the_outer_principal() -> None:
    """An inner binding unwinds to the outer one, not to the default."""
    outer = Principal(subject="alice", auth_method="token")
    inner = Principal(subject="bob", auth_method="token")
    observed: list[Principal] = []
    with bind_principal(outer):
        with bind_principal(inner):
            observed.append(current_principal())
        observed.append(current_principal())
    observed.append(current_principal())
    assert observed == [inner, outer, LOCAL_OPERATOR]


def test_binding_is_isolated_from_other_threads() -> None:
    """A binding in one thread is invisible to another thread."""
    observed: list[Principal] = []

    def worker() -> None:
        observed.append(current_principal())

    with bind_principal(Principal(subject="alice", auth_method="token")):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert observed == [LOCAL_OPERATOR]


def test_anonymous_subject() -> None:
    """ANONYMOUS names the caller the transport refused to identify."""
    assert ANONYMOUS.subject == "anonymous"


def test_anonymous_has_no_roles() -> None:
    """ANONYMOUS carries no authority at all."""
    assert not ANONYMOUS.roles


def test_anonymous_has_no_scopes() -> None:
    """ANONYMOUS carries no scopes at all."""
    assert not ANONYMOUS.scopes


def test_anonymous_auth_method() -> None:
    """ANONYMOUS records that no authentication happened."""
    assert ANONYMOUS.auth_method == "none"


def test_anonymous_is_not_the_local_operator() -> None:
    """An unauthenticated caller must never be treated as the local operator."""
    assert ANONYMOUS != LOCAL_OPERATOR


def test_anonymous_is_a_legal_principal() -> None:
    """ANONYMOUS passes Principal.__post_init__ with empty roles and scopes."""
    assert Principal(subject="anonymous", auth_method="none") == ANONYMOUS


def test_no_mcp_tool_takes_an_identity_shaped_parameter() -> None:
    """A tool must never let the caller name themselves.

    Attribution comes from the principal the transport bound for the request,
    not from a tool argument: whoever holds the credential could pass any
    ``actor`` value, so an identity-shaped parameter would make every record
    it stamps forgeable. This guard stops a future story from "fixing"
    attribution by adding one.
    """
    import pipeline.server

    forbidden = {"actor", "principal", "decided_by", "user", "whoami"}
    assert not any(
        forbidden & set(inspect.signature(tool.fn).parameters)
        for tool in pipeline.server.mcp._tool_manager._tools.values()
    )
