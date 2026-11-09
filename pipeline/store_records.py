"""Namespaced record store contract (ADR 0012, stage A).

The record store API is made of *records*, never paths, and every operation
takes ``namespace`` as its first argument.  This module defines the contract
only: it holds no storage code and mutates no state.
"""

from __future__ import annotations

import math
import re
from typing import Protocol

DEFAULT_NAMESPACE = 'default'

#: The complete, closed field set of a usage record (ADR 0012 decision 3,
#: ``docs/TELEMETRY_CONTRACT.md`` "Usage record").  Declared here, in the
#: contract, and aliased by :data:`pipeline.usage_record.USAGE_FIELDS`, so the
#: accounting artefact and the store that stores it cannot drift apart.  A
#: record carries counts, ids and cost -- never prompts, completions or any
#: other payload.
USAGE_RECORD_FIELDS: tuple[str, ...] = (
    'ts',
    'plan',
    'story_key',
    'attempt',
    'correlation_id',
    'role',
    'backend',
    'model',
    'served_model',
    'input_tokens',
    'output_tokens',
    'cache_creation_input_tokens',
    'cache_read_input_tokens',
    'cost_usd',
    'duration_ms',
)

#: The four token counts, which are non-negative ints and never bools.
_USAGE_TOKEN_FIELDS = (
    'input_tokens',
    'output_tokens',
    'cache_creation_input_tokens',
    'cache_read_input_tokens',
)

#: Fields that are a string or ``None``: ids the provider may not report.
_USAGE_OPTIONAL_STRING_FIELDS = ('story_key', 'correlation_id', 'served_model')

# A namespace is an opaque, lower-case key chosen by the caller.  Core never
# attaches meaning to it and always passes DEFAULT_NAMESPACE.
_NAMESPACE_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')

# Plan names and story keys are flat names, never path fragments.
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

# The outbox channel key of the legacy e-mail spool (``<plan>.outbox.jsonl``).
# The leading underscore is forbidden by the instance-name rule below, so this
# key can never collide with an operator's notification channel instance.
LEGACY_OUTBOX_CHANNEL = '_legacy'

# Outbox channel keys reuse notification_channels' instance-name rule, so a
# channel is directly addressable as an instance name.
_CHANNEL_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')


class RecordStoreError(Exception):
    """Base class for record store contract violations."""


class UnknownNamespaceError(RecordStoreError, LookupError):
    """Raised when an implementation does not know the requested namespace."""


class InvalidRecordKeyError(RecordStoreError, ValueError):
    """Raised when a namespace, plan name or story key is not a valid key."""


class InvalidUsageRecordError(RecordStoreError, ValueError):
    """Raised when a usage record is not a valid usage record.

    The message names the offending *field* and never its value: a usage
    record's values include token counts and cost, and an error raised on the
    way to storage must not become a second, unvalidated copy of the record.
    """


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


def _usage_number(value: object) -> bool:
    """Return whether ``value`` is a real number (``bool`` is not one)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _reject(field: str, detail: str) -> InvalidUsageRecordError:
    return InvalidUsageRecordError(f'invalid usage record field {field!r}: {detail}')


def validate_usage_record(plan_name: str, record: dict) -> dict:
    """Return a shallow copy of ``record`` when it is a valid usage record.

    A usage record is an accounting artefact, so its field set is closed: the
    keys must be exactly :data:`USAGE_RECORD_FIELDS`, and every value must be a
    value that field may hold.  Validation happens before anything is written,
    so a rejected record leaves the plan's usage file untouched.

    Messages name the offending field only, never its value.
    """
    if not isinstance(record, dict):
        raise TypeError('record must be a dict')
    for field in USAGE_RECORD_FIELDS:
        if field not in record:
            raise _reject(field, 'missing')
    for field in record:
        if field not in USAGE_RECORD_FIELDS:
            raise _reject(field, 'not a usage record field')

    if not isinstance(record['ts'], str):
        raise _reject('ts', 'not a timestamp string')
    if record['plan'] != plan_name:
        raise _reject('plan', 'does not match the plan it is stored under')
    for field in _USAGE_TOKEN_FIELDS:
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _reject(field, 'not a non-negative token count')
    for field in ('cost_usd', 'duration_ms'):
        value = record[field]
        if value is None:
            continue
        if not _usage_number(value) or value < 0 or not math.isfinite(value):
            raise _reject(field, 'not a non-negative finite measure or None')
    attempt = record['attempt']
    if attempt is not None and (
        not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0
    ):
        raise _reject('attempt', 'not a non-negative attempt number or None')
    for field in ('role', 'backend', 'model'):
        if not isinstance(record[field], str):
            raise _reject(field, 'not a string')
    for field in _USAGE_OPTIONAL_STRING_FIELDS:
        value = record[field]
        if value is not None and not isinstance(value, str):
            raise _reject(field, 'not a string or None')
    return dict(record)

def validate_channel(value: str) -> str:
    """Return ``value`` when it is a valid outbox channel key, else raise.

    A channel key is either the legacy e-mail spool key
    (:data:`LEGACY_OUTBOX_CHANNEL`) or a notification channel instance name.
    The legacy key starts with an underscore, which the instance-name rule
    forbids, so an operator instance called ``default`` or ``legacy`` can never
    collide with it.

    The error message is deliberately generic: the offending value is never
    echoed back.
    """
    if value == LEGACY_OUTBOX_CHANNEL:
        return value
    if not isinstance(value, str) or _CHANNEL_RE.fullmatch(value) is None:
        raise InvalidRecordKeyError('invalid channel')
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

    def get_manifests(self, namespace: str) -> dict[str, dict]:
        """Return ``{plan_name: manifest}`` for every readable manifest.

        One bulk read, so a caller that needs every manifest does not pay for a
        ``list_plans`` + ``get_manifest`` loop.  The keys are the plan names in
        sorted order; a namespace with no content yields ``{}``.

        A manifest that cannot be read, or that is not a JSON object, is
        skipped rather than raised, and the failure is logged at ``WARNING``
        once per distinct failure: one bad file must not blind a caller that
        scans every plan.

        **The returned documents are read-only snapshots.** Implementations may
        share parsed objects between calls (the file store does, through its
        size-and-mtime parse cache), so a caller must not mutate them; a caller
        that needs to mutate a manifest calls :meth:`get_manifest`, which
        returns an independent copy.

        The namespace is validated with ``validate_namespace`` and an unknown
        namespace is refused exactly like the other operations, before storage
        is touched.
        """
        ...

    def put_manifest(self, namespace: str, plan_name: str, manifest: dict) -> None: ...

    def list_plans(self, namespace: str) -> list[str]: ...

    def get_decisions(self, namespace: str, plan_name: str) -> list[dict]: ...

    def append_decision(self, namespace: str, plan_name: str, record: dict) -> None: ...

    def get_journal(self, namespace: str, plan_name: str, story_key: str) -> list[dict]: ...

    def append_journal(
        self, namespace: str, plan_name: str, story_key: str, record: dict
    ) -> None: ...

    def clear_journal(
        self, namespace: str, plan_name: str, story_key: str
    ) -> None: ...

    def get_journal_updated_at(
        self, namespace: str, plan_name: str, story_key: str
    ) -> float | None:
        """Return when ``story_key``'s journal was last appended to.

        The answer is epoch seconds, monotonic non-decreasing across appends to
        one journal, and ``None`` when the story has no journal -- never
        appended to, or cleared since.  Activity is therefore a property of the
        journal, not of the records inside it: a journal whose records carry no
        ``ts`` still has activity.  A read, so it creates nothing on disk.
        """
        ...

    def list_journal_keys(self, namespace: str, plan_name: str) -> list[str]:
        """Return the sorted story keys that have a journal in ``plan_name``.

        Only keys valid for ``plan_name`` are listed: a plan name that is a
        prefix of another plan's name (``p`` of ``p.x``) must not leak the other
        plan's journals, and a cleared journal is not listed.  An unknown plan
        is an empty list, not an error.
        """
        ...

    def append_usage(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one usage record to ``plan_name``'s usage file.

        Usage records are append-only by construction: the contract has no
        update and no delete, so an accounting line, once written, is never
        rewritten in place.  ``record``'s keys must be exactly
        :data:`USAGE_RECORD_FIELDS` and every value must be one the field may
        hold (:func:`validate_usage_record`); a rejected record is refused
        before anything is written.
        """
        ...

    def get_usage(self, namespace: str, plan_name: str) -> list[dict]:
        """Return ``plan_name``'s usage records, in the order they were written.

        A read, so it creates nothing on disk, and an absent usage file is an
        empty list rather than an error.
        """
        ...

    def transaction(self, namespace: str, plan_name: str, wait_seconds: float = 0.0):
        """Serialise mutations of one ``(namespace, plan_name)`` pair.

        A context manager that yields whether the lock was acquired, and never        blocks longer than ``wait_seconds``.  Callers MUST check the yielded
        bool and skip all work when it is ``False``: this is the scheduler's
        "skip this tick" path, and it must stay cheap.  ``wait_seconds`` is
        opt-in -- the default ``0.0`` is a single attempt with no sleeping.

        Re-entrant on the holding thread, so nested call sites compose instead
        of deadlocking against themselves.  ``namespace`` and ``plan_name`` are
        validated first and an unknown namespace is refused, both *before* any
        lock is taken.  This story defines the lock only: atomicity of the
        writes made inside the block is a later decision.
        """
        ...


RECORD_STORE_METHODS = (
    'get_manifest',
    'get_manifests',
    'put_manifest',
    'list_plans',
    'get_decisions',
    'append_decision',
    'append_usage',
    'get_usage',
    'get_journal',
    'append_journal',
    'clear_journal',
    'get_journal_updated_at',
    'list_journal_keys',
    'transaction',
)
