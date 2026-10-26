"""Tests for the request correlation seam (``pipeline.actor``).

The actor seam answers *who* is asking; the correlation seam answers *which
request* it is. These tests pin the binding semantics (bind, restore, nest,
thread isolation), the minted-id shape, and the rule that a caller-controlled
inbound id is only accepted when it is well formed -- echoing an arbitrary
header into a response header and into logs is log forging.
"""

from __future__ import annotations

import re
import threading

from app.identity import Principal
from pipeline.actor import (
    CORRELATION_ID_RE,
    actor_id,
    bind_correlation_id,
    bind_principal,
    current_correlation_id,
    new_correlation_id,
    request_correlation_id,
)


def test_unbound_correlation_id_is_empty_string() -> None:
    """Outside a request the id is "" -- the record fields expect a string."""
    assert current_correlation_id() == ""


def test_bound_correlation_id_is_visible_inside_the_block() -> None:
    """A transport's correlation id is what every layer reads."""
    with bind_correlation_id("req-1"):
        assert current_correlation_id() == "req-1"


def test_correlation_binding_is_restored_after_the_block() -> None:
    """The binding must not leak past the request (a module global would)."""
    with bind_correlation_id("req-1"):
        pass
    assert current_correlation_id() == ""


def test_nested_correlation_binding_restores_the_outer_id() -> None:
    """An inner binding unwinds to the outer one, not to the default."""
    with bind_correlation_id("outer-id"):
        with bind_correlation_id("inner-id"):
            pass
        assert current_correlation_id() == "outer-id"


def test_correlation_binding_is_isolated_from_other_threads() -> None:
    """A binding in one thread is invisible to another thread."""
    observed: list[str] = []

    def worker() -> None:
        observed.append(current_correlation_id())

    with bind_correlation_id("req-1"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert observed == [""]


def test_new_correlation_id_is_twelve_lowercase_hex_characters() -> None:
    """The minted shape matches the per-story id dispatch already uses."""
    assert re.fullmatch(r"[0-9a-f]{12}", new_correlation_id())


def test_new_correlation_id_differs_between_calls() -> None:
    """Two requests must never share an id."""
    assert new_correlation_id() != new_correlation_id()


def test_binding_a_principal_alone_leaves_the_correlation_id_empty() -> None:
    """The two seams are independent: binding an actor mints no request id."""
    with bind_principal(Principal(subject="alice", auth_method="token")):
        assert current_correlation_id() == ""


def test_binding_a_principal_alone_still_sets_the_actor() -> None:
    """Adding the correlation seam must not disturb actor propagation."""
    principal = Principal(subject="alice", auth_method="token")
    with bind_principal(principal):
        assert actor_id() == principal.subject


def test_binding_a_correlation_id_alone_leaves_the_actor_local() -> None:
    """Binding a request id does not authenticate anybody."""
    with bind_correlation_id("req-1"):
        assert actor_id() == "local-operator"


def test_request_correlation_id_mints_when_none_is_supplied() -> None:
    """No inbound header still yields a usable id."""
    assert (minted := request_correlation_id(None)) and CORRELATION_ID_RE.match(minted)


def test_request_correlation_id_mints_when_empty_is_supplied() -> None:
    """An empty header is not an id."""
    assert (minted := request_correlation_id("")) and CORRELATION_ID_RE.match(minted)


def test_request_correlation_id_preserves_a_well_formed_upstream_id() -> None:
    """A caller's well-formed id is kept so its logs can be joined."""
    assert request_correlation_id("abc-123") == "abc-123"


def test_request_correlation_id_replaces_a_forged_value() -> None:
    """A newline in the header would forge log lines, so it is replaced."""
    assert (minted := request_correlation_id("bad id\ninjected")) and (
        CORRELATION_ID_RE.match(minted) and "injected" not in minted
    )


def test_request_correlation_id_replaces_a_value_with_a_trailing_newline() -> None:
    """``$`` also matches before a final newline, so the guard must fullmatch."""
    assert (minted := request_correlation_id("abc\n")) and (
        CORRELATION_ID_RE.fullmatch(minted) and minted != "abc\n"
    )


def test_request_correlation_id_replaces_an_over_long_value() -> None:
    """An unbounded header would bloat every log line, so it is replaced."""
    assert (minted := request_correlation_id("a" * 65)) and (
        CORRELATION_ID_RE.match(minted) and minted != "a" * 65
    )
