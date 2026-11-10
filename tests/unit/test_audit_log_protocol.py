"""The audit-log surface: append and read only, never rewrite (ADR 0012).

The audit trail is only trustworthy while it cannot be rewritten from the
inside.  These tests pin the *shape* of the surface: the protocol declares
exactly the four operations an append-only log has, and no public name on the
protocol or on the reference implementation reads like a mutator.  A later
story that adds a mutator fails here.
"""

from __future__ import annotations

import re
from typing import Protocol

import pytest

from pipeline import audit_log, audit_memory
from pipeline.store_records import InvalidRecordKeyError, validate_plan_name

#: Any public name that reads like "this changes or drops stored history".
MUTATOR_PATTERN = re.compile(
    r"(update|delete|remove|replace|truncate|rewrite|put|set|clear|drop|pop)"
)

#: The operations an append-only audit log has.
REQUIRED_METHODS = frozenset({"append", "read_chain", "head", "list_chains"})


def _public_callables(cls: type) -> set[str]:
    """Return the public callables *cls* declares."""
    return {
        name
        for name, value in vars(cls).items()
        if not name.startswith("_") and callable(value)
    }


def _public_attribute_names(obj: object) -> list[str]:
    """Return the public attribute names *obj* carries."""
    return [name for name in dir(obj) if not name.startswith("_")]


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


def test_the_audit_log_is_a_protocol():
    assert Protocol in audit_log.AuditLog.__mro__


def test_the_audit_log_methods_are_the_four_operations():
    assert REQUIRED_METHODS <= set(audit_log.AUDIT_LOG_METHODS)


def test_the_protocol_declares_exactly_the_audit_log_methods():
    assert _public_callables(audit_log.AuditLog) == set(audit_log.AUDIT_LOG_METHODS)


def test_no_protocol_method_reads_like_a_mutator():
    for name in audit_log.AUDIT_LOG_METHODS:
        assert not MUTATOR_PATTERN.search(name), name


def test_the_reference_implementation_offers_the_protocol_methods():
    log = audit_memory.InMemoryAuditLog()
    for name in audit_log.AUDIT_LOG_METHODS:
        assert callable(getattr(log, name)), name


def test_no_public_attribute_of_the_reference_implementation_reads_like_a_mutator():
    log = audit_memory.InMemoryAuditLog()
    for name in _public_attribute_names(log):
        assert not MUTATOR_PATTERN.search(name), name


# --------------------------------------------------------------------------
# The host chain key
# --------------------------------------------------------------------------


def test_the_host_chain_id_is_the_reserved_at_host_key():
    assert audit_log.HOST_CHAIN_ID == "@host"


def test_the_host_chain_id_can_never_be_a_plan_name():
    with pytest.raises(InvalidRecordKeyError):
        validate_plan_name(audit_log.HOST_CHAIN_ID)


def test_validate_chain_accepts_the_host_chain_id():
    assert audit_log.validate_chain(audit_log.HOST_CHAIN_ID) == audit_log.HOST_CHAIN_ID


def test_validate_chain_accepts_a_valid_plan_name():
    assert audit_log.validate_chain("plan-a") == "plan-a"


@pytest.mark.parametrize("chain", ["../x", "", "a/b", "host:x", "host"])
def test_validate_chain_refuses_a_chain_that_is_not_a_plan_name(chain):
    with pytest.raises(InvalidRecordKeyError):
        audit_log.validate_chain(chain)


@pytest.mark.parametrize("chain", ["../x", "a/b", "host:x", "host"])
def test_the_refusal_message_never_echoes_the_offending_chain(chain):
    with pytest.raises(InvalidRecordKeyError) as caught:
        audit_log.validate_chain(chain)
    message = str(caught.value)
    assert message
    assert chain not in message
