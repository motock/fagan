"""Namespaced record store contract (ADR 0012, stage A).

The record store API is made of *records*, never paths, and every operation
takes ``namespace`` as its first argument.  This module defines the contract
and its validators; the only implementation it carries is
:class:`PlanDraftRecords`, the file-layout plan-draft mixin that
``pipeline/store.py`` inherits, because that module sits on the 1000-line gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Protocol

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

#: Names no plan may take.  The audit anchor log keys the host chain as "host",
#: so a plan of that name would share its anchor key with the host chain.
RESERVED_PLAN_NAMES = frozenset({'host'})

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


class UnreadableRecordError(RecordStoreError, ValueError):
    """Raised when a stored record exists but cannot be read back.

    Missing is not unreadable: a record that is not there is ``None`` (or an
    empty list, or ``False`` when it was deleted), while a record that is there
    and malformed is a failure a caller must see.  ``reason`` carries the
    refusal text the legacy reader produces for the same file
    (:func:`pipeline.persistence.read_plan_file`), so a message built from this
    error stays byte-identical to the one ingest already prints.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_namespace(value: str) -> str:
    """Return ``value`` when it is a valid namespace, else raise.

    The error message is deliberately generic: the offending value may be a
    path and is never echoed back.
    """
    if not isinstance(value, str) or _NAMESPACE_RE.fullmatch(value) is None:
        raise InvalidRecordKeyError('invalid namespace')
    return value


def validate_plan_name(value: str) -> str:
    """Return ``value`` when it is a valid plan name, else raise.

    ``"host"`` is reserved: the audit anchor log keys the host chain as
    ``host`` (``pipeline/audit_anchor.py`` ``HOST_CHAIN``), so a plan of that
    name would make the anchor log ambiguous.
    """
    if (
        not isinstance(value, str)
        or _KEY_RE.fullmatch(value) is None
        or '..' in value
    ):
        raise InvalidRecordKeyError('invalid plan name')
    if value in RESERVED_PLAN_NAMES:
        raise InvalidRecordKeyError('reserved plan name')
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


# A context snapshot is content-addressed: its key is ``sha256:`` followed by
# the lower-case hex SHA-256 of the content, and nothing else is a key.  A
# digest is therefore never a path fragment and can never address anything but
# its own blob -- a traversal attempt fails the format check, not a path check.
_CONTEXT_DIGEST_RE = re.compile(r'^sha256:([0-9a-f]{64})$')


def context_digest(content: str) -> str:
    """Return the canonical digest of *content* (``sha256:<hex>``)."""
    if not isinstance(content, str):
        raise TypeError('context snapshot content must be a str')
    return 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()


def validate_context_digest(value: object) -> str:
    """Return the hex part of a well-formed context digest, else raise.

    Only ``sha256:`` + 64 lower-case hex characters is a digest: a bare hex
    string, an uppercase or non-hex body, or anything carrying a path fragment
    is refused with :class:`InvalidRecordKeyError` before any storage is
    touched.
    """
    if not isinstance(value, str):
        raise InvalidRecordKeyError('invalid context digest')
    match = _CONTEXT_DIGEST_RE.fullmatch(value)
    if match is None:
        raise InvalidRecordKeyError('invalid context digest')
    return match.group(1)


def verify_context_digest(digest: str, content: str) -> str:
    """Return the hex part of *digest* after checking it addresses *content*.

    A malformed digest is refused with :class:`InvalidRecordKeyError`; a
    well-formed digest that is not the SHA-256 of *content* is refused with
    :class:`ValueError`, because the key would then name another blob.
    """
    hexdigest = validate_context_digest(digest)
    if context_digest(content) != digest:
        raise ValueError('context digest does not match the content')
    return hexdigest


def snapshot_meta(meta: dict | None, digest: str, content: str) -> dict:
    """Return the snapshot's stored meta: the caller's plus its address."""
    if meta is not None and not isinstance(meta, dict):
        raise TypeError('context snapshot meta must be a dict')
    return {
        **(meta or {}),
        'digest': digest,
        'bytes': len(content.encode('utf-8')),
    }


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


def validate_notification_record(record: object) -> None:
    """Refuse anything that is not a notification record, before any write.

    A notification record is a dict carrying a string ``ts`` and a
    ``message`` that is a string or ``None``.  The timestamp and the message
    are what the free-text line (``f'{ts} {message}'``) both on-disk
    representations of a notification are keyed on, so a record without them
    has nothing to write.  ``None`` stays allowed because the writers in this
    family build the record with :func:`pipeline.persistence._notification_record`,
    which leaves ``message`` as ``None`` for a notification that carries no
    message -- refusing it here would drop a notification the pipeline has
    always recorded.  Every other shape -- a non-dict, a missing or non-string
    timestamp, a missing message or a message that is neither a string nor
    ``None`` -- raises :class:`TypeError` here, before any storage is touched.
    """
    if not isinstance(record, dict):
        raise TypeError('notification record must be a dict')
    if not isinstance(record.get('ts'), str):
        raise TypeError('notification record must have a string ts')
    if 'message' not in record:
        raise TypeError('notification record must have a message')
    if record['message'] is not None and not isinstance(record['message'], str):
        raise TypeError('notification record message must be a string or None')


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

    def append_notification(self, namespace: str, plan_name: str, record: dict) -> None:
        """Append one notification ``record`` to ``plan_name``'s history.

        The record is a dict carrying a string ``ts`` and a ``message`` that is
        a string or ``None``; anything else raises :class:`TypeError` before
        storage is touched.  The
        append is durable for the store's own lifetime: the next read of the
        history serves it.  Keys are validated and an unknown namespace is
        refused before storage is touched.
        """
        ...

    def get_notification_history(
        self, namespace: str, plan_name: str
    ) -> tuple[list[dict], int]:
        """Return ``plan_name``'s notification records and a malformed count.

        The records are in written order, oldest first.  The second element
        counts the lines of the plan's notification sidecar that could not be
        read as a record; a row store that keeps the history itself has no such
        sidecar and always reports ``0``.  (The transitional ``SqliteRecords``
        view delegates to the file store until its own table lands, so it
        reports the sidecar's count.)  A plan with no notifications yields
        ``([], 0)``.  A read never mutates the history: it does not clear it,
        consume it or reorder it.
        """
        ...

    def get_notification_lines(
        self, namespace: str, plan_name: str, limit: int = 100
    ) -> list[str]:
        """Return ``plan_name``'s notification history as free-text lines.

        Oldest first, at most ``limit`` of them -- the most recent ones, each
        ``f'{ts} {message}'``.  A ``limit`` of zero or less means the last 100,
        like the legacy reader.  A read never mutates the history.
        """
        ...

    def put_plan_draft(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Stage ``plan`` as the transient plan draft of ``plan_name``.

        The draft is the plan document an operator hands the pipeline before
        ingest (ADR 0012 decision 3, "Plan draft (transient)
        ``<plan>.json``").  It replaces any previous draft *wholesale*: a
        field absent from ``plan`` is absent from the draft afterwards, never
        inherited from the document it replaced.

        ``plan`` must be a dict and is serialised before anything is written,
        so a document that cannot be stored leaves no trace behind.  The
        namespace and plan name are validated first; an unknown namespace is
        refused before storage is touched.
        """
        ...

    def get_plan_draft(self, namespace: str, plan_name: str) -> dict | None:
        """Return ``plan_name``'s staged plan draft, or ``None`` when absent.

        A read, so it creates nothing.  Missing is not unreadable: a plan with
        no draft is ``None``, while a draft that exists but is malformed, is
        not a JSON object, or cannot be read raises
        :class:`UnreadableRecordError` whose ``reason`` is the refusal text
        the legacy reader (:func:`pipeline.persistence.read_plan_file`)
        produces for the same file.  A truncated write must never surface as a
        bare ``JSONDecodeError``: ingest builds its refusal message from this
        error and that message stays byte-identical.
        """
        ...

    def delete_plan_draft(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s plan draft; return whether one was deleted.

        ``False`` -- not an error -- when there was no draft to delete, so the
        call is idempotent and a second delete reports ``False``.
        """
        ...

    def list_plan_drafts(self, namespace: str) -> list[str]:
        """Return the sorted plan names that have a staged draft.

        Only ``<plan>.json`` files whose stem holds no ``'.'`` count, so a
        plan's manifest, decisions and journal sidecars are not drafts, and
        the file must hold a JSON object with an ``epics`` key -- the same
        filter :meth:`pipeline.service.PipelineService.list_ingestable_plans`
        applies.  A file that cannot be parsed is skipped, not raised: one bad
        draft must not blind a caller that lists them all.
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

    def mark_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Create ``plan_name``'s once-only completion marker, atomically.

        Returns ``True`` only for the call that created the marker, ``False``
        when it was already there -- the create is a single atomic
        ``O_CREAT|O_EXCL`` (or the backend's equivalent), so two racing ticks
        cannot both report completion.

        This is the durable record behind the legacy completion marker file in
        the plans directory: one marker, one notice (ADR 0012 decision 3).
        """
        ...

    def is_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Whether ``plan_name``'s completion marker exists."""
        ...

    def put_plan_report(self, namespace: str, plan_name: str, text: str) -> None:
        """Store ``plan_name``'s report, replacing any previous one wholesale.

        The report is the plan-completion summary retros read back; it is a
        record, never the delivery.  A non-``str`` report is refused with
        :class:`TypeError` before anything is written.
        """
        ...

    def get_plan_report(self, namespace: str, plan_name: str) -> str | None:
        """Return ``plan_name``'s report, or ``None`` when it has none."""
        ...

    def put_context_snapshot(
        self, namespace: str, digest: str, content: str, meta: dict
    ) -> None:
        """Store the context snapshot ``digest`` addresses, once and for all.

        A snapshot is a content-addressed, immutable blob: ``digest`` is
        ``'sha256:' + sha256(content)``, a digest that addresses other content
        is refused with :class:`ValueError`, a malformed one with
        :class:`InvalidRecordKeyError`, and a second put of an existing digest
        is a no-op that never overwrites the stored content or meta.  There is
        no delete and no listing.
        """
        ...

    def get_context_snapshot(self, namespace: str, digest: str) -> dict | None:
        """Return ``{'content', 'meta'}`` for ``digest``, or ``None``.

        ``None`` means the snapshot is not stored -- never an error -- so a
        caller can refetch and re-store it; a malformed digest is refused with
        :class:`InvalidRecordKeyError` instead of being looked up.
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
    'append_notification',
    'get_notification_history',
    'get_notification_lines',
    'put_plan_draft',
    'get_plan_draft',
    'delete_plan_draft',
    'list_plan_drafts',
    'mark_plan_completed',
    'is_plan_completed',
    'put_plan_report',
    'get_plan_report',
    'put_context_snapshot',
    'get_context_snapshot',
    'transaction',
)


class PlanDraftRecords:
    """The transient plan-draft operations, mixed into the file record view.

    A *plan draft* is the staged plan document an operator hands the pipeline
    before ingest (ADR 0012 decision 3, "Plan draft (transient) ``<plan>.json``").
    It is not durable store state: it is the plan JSON ``save_plan`` writes and
    ingest reads, so the file store keeps today's layout and the many callers
    that write a plan file by hand keep working.

    The file-layout implementation lives in this module, next to the contract it
    satisfies, because ``pipeline/store.py`` sits on the 1000-line gate and a
    record kind with real behaviour cannot grow there.

    The host class supplies ``_store`` (the legacy file store, whose
    ``manifest_path`` resolves the plans directory per call) and
    ``_refuse_other_namespace`` (this view serves :data:`DEFAULT_NAMESPACE`).
    """

    #: Provided by the host record view.
    _store: Any

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _draft_path(self, plan_name: str) -> Path:
        """Return ``<plans dir>/<plan>.json``, resolved per call.

        The plans directory comes from the file store's own ``manifest_path``,
        which resolves the server module's plans-directory binding through a
        :class:`pipeline.live_ref.LiveRef` at call time, so a plans directory
        patched on that module is the one the draft lands in.
        """
        return self._store.manifest_path(plan_name).parent / f"{plan_name}.json"

    def put_plan_draft(self, namespace: str, plan_name: str, plan: dict) -> None:
        """Stage ``plan`` as ``plan_name``'s transient plan draft.

        The layout is the one ingest already reads -- ``<plan>.json`` in the
        plans directory -- so a plan handed over by hand and a plan handed over
        by ``save_plan`` land in the same place, and the draft replaces any
        previous one wholesale.

        The write goes through ``pipeline.server._atomic_write_json`` resolved
        per call (the module attribute, never an import-time copy), so a patched
        writer is the one the write goes through and ``save_plan``'s staging and
        this call share a single writer.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        if not isinstance(plan, dict):
            raise TypeError('plan must be a dict')
        from pipeline import server

        server._atomic_write_json(self._draft_path(plan_name), plan)

    def get_plan_draft(self, namespace: str, plan_name: str) -> dict | None:
        """Return ``plan_name``'s plan draft, or ``None`` when it has none.

        Read through :func:`pipeline.persistence.read_plan_file`, the reader
        ingest already refuses with, so a malformed or unreadable draft raises
        :class:`UnreadableRecordError` carrying that reader's own reason text
        and ingest's refusal messages stay byte-identical.  Missing is not
        unreadable: an absent draft is ``None``.  Nothing is cached, so a draft
        rewritten on disk is read afresh on the next call.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        path = self._draft_path(plan_name)
        if not path.is_file():
            return None
        from pipeline import persistence

        plan, refusal = persistence.read_plan_file(path)
        if refusal is not None:
            raise UnreadableRecordError(refusal['error'])
        return plan

    def delete_plan_draft(self, namespace: str, plan_name: str) -> bool:
        """Delete ``plan_name``'s draft; report whether one was deleted.

        ``False`` -- not an error -- when there was nothing to delete, so the
        call is idempotent and a second delete reports ``False``.
        """
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)
        try:
            self._draft_path(plan_name).unlink()
        except FileNotFoundError:
            return False
        return True

    def list_plan_drafts(self, namespace: str) -> list[str]:
        """Return the sorted plan names that have an ingestable draft.

        Only ``<plan>.json`` files count: a stem holding a ``'.'`` is a
        manifest, decisions or journal sidecar, and the file must hold a JSON
        object with an ``epics`` key -- the same filter
        :meth:`pipeline.service.PipelineService.list_ingestable_plans` applies.
        A file that cannot be read or parsed is skipped rather than raised: one
        bad draft must not blind a caller that lists them all.
        """
        validate_namespace(namespace)
        self._refuse_other_namespace(namespace)
        drafts = []
        for path in self._draft_path('draft').parent.glob('*.json'):
            if '.' in path.stem:
                continue
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(document, dict) and 'epics' in document:
                drafts.append(path.stem)
        return sorted(drafts)


class PlanCompletionRecords:
    """The plan-completion marker and report, mixed into the file record view.

    Plan completion leaves two durable artefacts behind (ADR 0012 decision 3):
    the once-only ``<plan>.plan_completed`` marker and the ``<plan>.report.md``
    summary retros read back.  The file layout is unchanged, so a caller that
    reads either file directly keeps seeing what it saw before.

    The file-layout implementation lives here, next to the contract it
    satisfies, because ``pipeline/store.py`` sits on the 1000-line gate.

    The host class supplies ``_store`` (the legacy file store, whose
    ``manifest_path`` resolves the plans directory per call) and
    ``_refuse_other_namespace`` (this view serves :data:`DEFAULT_NAMESPACE`).
    """

    #: Provided by the host record view.
    _store: Any

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _plan_path(self, plan_name: str, suffix: str) -> Path:
        """Return the plans directory ``plan_name``'s ``suffix`` file lives in.

        The directory comes from the file store's own ``manifest_path``, which
        resolves the plans directory per call, so a plans directory patched on
        the server module is the one the record lands in.
        """
        return self._store.manifest_path(plan_name).parent / f'{plan_name}{suffix}'

    def _validate(self, namespace: str, plan_name: str) -> None:
        """Validate ``namespace`` and ``plan_name`` before anything is written."""
        validate_namespace(namespace)
        validate_plan_name(plan_name)
        self._refuse_other_namespace(namespace)

    def mark_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Create ``plan_name``'s marker with ``O_CREAT|O_EXCL``; True if created.

        The exclusive create is the atomicity: two racing callers both open the
        same path, exactly one succeeds, and the loser gets ``FileExistsError``
        and reports ``False`` -- no read-then-write window, so the plan is
        announced once.
        """
        self._validate(namespace, plan_name)
        path = self._plan_path(plan_name, '.plan_completed')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def is_plan_completed(self, namespace: str, plan_name: str) -> bool:
        """Whether ``plan_name``'s completion marker exists."""
        self._validate(namespace, plan_name)
        return self._plan_path(plan_name, '.plan_completed').exists()

    def put_plan_report(self, namespace: str, plan_name: str, text: str) -> None:
        """Write ``plan_name``'s report to ``<plan>.report.md``, wholesale."""
        self._validate(namespace, plan_name)
        if not isinstance(text, str):
            raise TypeError('plan report must be a str')
        path = self._plan_path(plan_name, '.report.md')
        tmp = path.with_name(f'{path.name}.tmp')
        tmp.write_text(text, encoding='utf-8')
        try:
            os.replace(tmp, path)
        except OSError:
            # Leave nothing behind: a failed replace must not litter the plans
            # directory with a half-written report.
            tmp.unlink(missing_ok=True)
            raise

    def get_plan_report(self, namespace: str, plan_name: str) -> str | None:
        """Return ``plan_name``'s report, or ``None`` when it has none."""
        self._validate(namespace, plan_name)
        path = self._plan_path(plan_name, '.report.md')
        if not path.is_file():
            return None
        return path.read_text(encoding='utf-8')


class ContextSnapshotRecords:
    """Content-addressed context snapshots, mixed into the file record view.

    A snapshot is the resolved text of a plan's external ``context`` source
    (ADR 0012 decision 11): an immutable, content-addressed blob whose key is
    the SHA-256 of its own content.  ``put`` refuses a digest that addresses
    other content, a second put of a stored digest is a no-op that never
    overwrites, and there is deliberately no delete and no listing -- a
    snapshot is only ever added to and read back.

    The file layout is unchanged (``PLAN_DIR/context/<hex>/content.txt`` plus
    ``meta.json``), so an operator and the tools that already read those two
    files keep working.  The implementation lives here, next to the contract it
    satisfies, because ``pipeline/store.py`` sits on the 1000-line gate.

    The host class supplies ``_refuse_other_namespace`` (this view serves
    :data:`DEFAULT_NAMESPACE`).
    """

    def _refuse_other_namespace(self, namespace: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _snapshots_root(self) -> Path:
        """Return the ``context`` directory, resolved per call.

        The directory is :data:`pipeline.paths.PLAN_DIR` read at call time, the
        same binding the context providers (``materialize_context_files``) and
        every caller that reads ``context/<hex>/`` already use, so a plans
        directory patched on that module is the one the snapshot lands in.
        """
        from . import paths

        return paths.PLAN_DIR / 'context'

    def _snapshot_folder(self, namespace: str, digest: object) -> Path:
        """Validate the keys and return the folder *digest* lives in."""
        validate_namespace(namespace)
        hexdigest = validate_context_digest(digest)
        self._refuse_other_namespace(namespace)
        return self._snapshots_root() / hexdigest

    def put_context_snapshot(
        self, namespace: str, digest: str, content: str, meta: dict
    ) -> None:
        """Store ``content`` under ``digest`` once; never overwrite it.

        The digest is checked against the content *before* the folder is even
        named, so a refused put writes nothing at all.
        """
        verify_context_digest(digest, content)
        folder = self._snapshot_folder(namespace, digest)
        payload = snapshot_meta(meta, digest, content)
        folder.mkdir(parents=True, exist_ok=True)
        # ``O_CREAT|O_EXCL`` is the immutability: exactly one put of a digest
        # can create its ``content.txt``, and every later put leaves it alone.
        try:
            fd = os.open(
                folder / 'content.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
            )
        except FileExistsError:
            return
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
        from pipeline import server

        server._atomic_write_json(folder / 'meta.json', payload)

    def get_context_snapshot(self, namespace: str, digest: str) -> dict | None:
        """Return ``{'content', 'meta'}`` for ``digest``, or ``None``."""
        folder = self._snapshot_folder(namespace, digest)
        try:
            content = (folder / 'content.txt').read_text(encoding='utf-8')
        except OSError:
            return None
        meta: dict = {}
        try:
            stored = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, dict):
            meta = stored
        return {'content': content, 'meta': meta}


class PlanRecords(
    PlanCompletionRecords, PlanDraftRecords, ContextSnapshotRecords
):
    """The whole plan-side record view, mixed into the file record view.

    A composition of the plan-lifecycle record kinds -- the transient plan
    draft, the plan-completion marker/report and the content-addressed
    context snapshot -- so ``pipeline/store.py``, which sits on the
    1000-line gate, names one base instead of one per kind.
    """

