"""Namespaced record store contract (ADR 0012, stage A).

The record store API is made of *records*, never paths, and every operation
takes ``namespace`` as its first argument.  This module defines the contract
only: it holds no storage code and mutates no state.
"""

from __future__ import annotations

import re
from typing import Protocol

DEFAULT_NAMESPACE = 'default'

# A namespace is an opaque, lower-case key chosen by the caller.  Core never
# attaches meaning to it and always passes DEFAULT_NAMESPACE.
_NAMESPACE_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')

# Plan names and story keys are flat names, never path fragments.
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


class RecordStoreError(Exception):
    """Base class for record store contract violations."""


class UnknownNamespaceError(RecordStoreError, LookupError):
    """Raised when an implementation does not know the requested namespace."""


class InvalidRecordKeyError(RecordStoreError, ValueError):
    """Raised when a namespace, plan name or story key is not a valid key."""


def validate_namespace(value: str) -> str:
    """Return ``value`` when it is a valid namespace, else raise.

    The error message is deliberately generic: the offending value may be a
    path and is never echoed back.
    """
    if not isinstance(value, str) or _NAMESPACE_RE.fullmatch(value) is None:
        raise InvalidRecordKeyError('invalid namespace')
    return value


def validate_plan_name(value: str) -> str:
    """Return ``value`` when it is a valid plan name, else raise."""
    if (
        not isinstance(value, str)
        or _KEY_RE.fullmatch(value) is None
        or '..' in value
    ):
        raise InvalidRecordKeyError('invalid plan name')
    return value


def validate_story_key(value: str) -> str:
    """Return ``value`` when it is a valid story key, else raise."""
    if (
        not isinstance(value, str)
        or _KEY_RE.fullmatch(value) is None
        or '..' in value
    ):
        raise InvalidRecordKeyError('invalid story key')
    return value


class RecordStore(Protocol):
    """The namespaced record store contract.

    Every operation deals in records, never in paths.  The ``namespace`` is an
    opaque key owned by the caller: core attaches no meaning to it and always
    passes :data:`DEFAULT_NAMESPACE`.  No operation ever spans namespaces —
    each call reads or writes exactly one namespace.

    Implementations must validate every key with the validators in this module
    (``validate_namespace``, ``validate_plan_name``, ``validate_story_key``)
    before touching storage, and a rejected call must not write anything.
    """

    def get_manifest(self, namespace: str, plan_name: str) -> dict | None: ...

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None: ...

    def list_plans(self, namespace: str) -> list[str]: ...

    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]: ...

    def append_decision(self, namespace: str, plan_name: str, record: dict) -> None: ...

    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]: ...

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None: ...


RECORD_STORE_METHODS = (
    'get_manifest',
    'put_manifest',
    'list_plans',
    'get_decisions',
    'append_decision',
    'get_journal',
    'append_journal',
)
