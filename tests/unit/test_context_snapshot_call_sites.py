"""Call-site tests for the context-snapshot records (ADR 0012 decision 11).

Two call sites move onto the record contract:

* ``pipeline.context_sources.snapshot_context`` writes a snapshot through
  ``put_context_snapshot`` instead of writing the file layout itself;
* ``pipeline.dispatch_context._load_recorded_context`` reads a recorded
  snapshot through ``get_context_snapshot`` instead of reading the file
  layout itself, and keeps today's behaviour: a snapshot that is gone is
  refetched and accepted only when it is byte-identical, and content that no
  longer matches the recorded digest is refused with the same
  ``ContextSourceError`` text.

The tests observe the public behaviour only: the record view is reached
through the store's own ``records()`` accessor, and the calls that go through
the record API are observed by wrapping the *contract's* methods, so no
implementation source text is read.
"""

from __future__ import annotations

import hashlib

import pytest

# pipeline.server must load before pipeline.service / pipeline.ingest (the
# import-cycle note in tests/unit/test_ingest_keyless_reingest.py).
import pipeline.server  # noqa: F401
from pipeline import context_sources as ctxsrc
from pipeline import dispatch_context
from pipeline import paths, persistence, server
from pipeline.store import FileRecords
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError

TEXT = '--- guide.md ---\nUSE-TABS-ALWAYS\n'
OTHER_TEXT = '--- guide.md ---\nSOMETHING-ELSE\n'
SOURCE = {'name': 'ux', 'provider': 'dir', 'uri': '/srv/docs'}


def digest_of(content: str) -> str:
    """Return the canonical digest of *content*."""
    return 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()


def _recorded(digest: str, content: str = TEXT) -> dict:
    """Return a ``context_digests`` entry for *content*."""
    return {'digest': digest, 'bytes': len(content.encode('utf-8'))}


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory wired into every module-level binding."""
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    monkeypatch.setattr(paths, 'PLAN_DIR', d)
    return d


@pytest.fixture
def record_calls(monkeypatch):
    """Record every context-snapshot call made through the record API."""
    calls: list[tuple[str, tuple, dict]] = []

    def _wrap(name: str) -> None:
        original = getattr(FileRecords, name, None)

        def spy(self, *args, **kwargs):
            calls.append((name, args, kwargs))
            if original is None:
                raise AssertionError(f'{name} is not implemented yet')
            return original(self, *args, **kwargs)

        monkeypatch.setattr(FileRecords, name, spy, raising=False)

    _wrap('put_context_snapshot')
    _wrap('get_context_snapshot')
    return calls


def _argument(call, index: int, *names: str):
    """Return the *index*-th positional argument of *call*, or a named one."""
    _name, args, kwargs = call
    if len(args) > index:
        return args[index]
    for candidate in names:
        if candidate in kwargs:
            return kwargs[candidate]
    raise AssertionError(f'no argument {names} in {call!r}')


def _calls_named(calls, name):
    """Return the recorded calls of *name*."""
    return [call for call in calls if call[0] == name]


def _refuse_refetch(monkeypatch):
    """Make any refetch of the source fail the test."""

    def no_refetch(source):
        raise AssertionError('a stored snapshot must be served, not refetched')

    monkeypatch.setattr(ctxsrc, 'resolve_dir_source', no_refetch)


def _fake_source(text: str):
    """Return a stand-in for ``resolve_dir_source`` serving *text*."""
    digest = digest_of(text)
    return {
        'text': text,
        'digest': digest,
        'bytes': len(text.encode('utf-8')),
        'files': 1,
        'truncated': False,
    }


# -- the write call site -------------------------------------------------


def test_snapshot_context_writes_through_put_context_snapshot(plan_dir, record_calls):
    """``snapshot_context`` stores the snapshot through the record API."""
    digest = ctxsrc.snapshot_context(TEXT)

    puts = _calls_named(record_calls, 'put_context_snapshot')
    assert puts, 'snapshot_context must write through put_context_snapshot'
    assert _argument(puts[0], 0, 'namespace') == DEFAULT_NAMESPACE
    assert _argument(puts[0], 1, 'digest') == digest
    assert _argument(puts[0], 2, 'content', 'text') == TEXT


def test_snapshot_context_returns_the_digest_of_the_content(plan_dir, record_calls):
    """``snapshot_context`` still answers with the snapshot's digest."""
    assert ctxsrc.snapshot_context(TEXT) == digest_of(TEXT)


def test_the_written_snapshot_is_readable_through_the_record_store(plan_dir):
    """What ``snapshot_context`` wrote is served by the record contract."""
    digest = ctxsrc.snapshot_context(TEXT)

    found = server._store.records().get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == TEXT


def test_snapshot_context_passes_the_caller_meta_through(plan_dir, record_calls):
    """The caller's meta reaches the record, not a copy of it."""
    ctxsrc.snapshot_context(TEXT, meta={'sha': 'abc123'})

    puts = _calls_named(record_calls, 'put_context_snapshot')
    assert puts, 'snapshot_context must write through put_context_snapshot'
    meta = _argument(puts[0], 3, 'meta')
    assert meta.get('sha') == 'abc123'


# -- the read call site --------------------------------------------------


def test_dispatch_serves_a_recorded_snapshot(plan_dir, record_calls, monkeypatch):
    """A recorded digest is served from the snapshot, without a refetch."""
    digest = digest_of(TEXT)
    server._store.records().put_context_snapshot(DEFAULT_NAMESPACE, digest, TEXT, {})
    _refuse_refetch(monkeypatch)

    loaded = dispatch_context._load_recorded_context(SOURCE, _recorded(digest))

    assert loaded['text'] == TEXT
    assert loaded['digest'] == digest
    assert loaded['bytes'] == len(TEXT.encode('utf-8'))


def test_dispatch_reads_the_recorded_digest_through_get_context_snapshot(
    plan_dir, record_calls, monkeypatch
):
    """The recorded digest is read through the record API."""
    digest = digest_of(TEXT)
    server._store.records().put_context_snapshot(DEFAULT_NAMESPACE, digest, TEXT, {})
    _refuse_refetch(monkeypatch)

    dispatch_context._load_recorded_context(SOURCE, _recorded(digest))

    reads = _calls_named(record_calls, 'get_context_snapshot')
    assert reads, 'the recorded digest must be read through get_context_snapshot'
    assert _argument(reads[0], 1, 'digest') == digest


def test_a_missing_snapshot_is_refetched(plan_dir, record_calls, monkeypatch):
    """A snapshot that is gone is refetched from the source."""
    digest = digest_of(TEXT)
    refetched = []

    def fake_resolve(source):
        refetched.append(source)
        return _fake_source(TEXT)

    monkeypatch.setattr(ctxsrc, 'resolve_dir_source', fake_resolve)

    loaded = dispatch_context._load_recorded_context(SOURCE, _recorded(digest))

    assert refetched, 'a missing snapshot must be refetched'
    assert loaded['text'] == TEXT
    assert loaded['digest'] == digest


def test_a_refetched_snapshot_is_stored_again(plan_dir, record_calls, monkeypatch):
    """The refetched content is snapshotted again, through the record API."""
    digest = digest_of(TEXT)
    monkeypatch.setattr(ctxsrc, 'resolve_dir_source', lambda source: _fake_source(TEXT))

    dispatch_context._load_recorded_context(SOURCE, _recorded(digest))

    found = server._store.records().get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert found is not None
    assert found['content'] == TEXT


def test_a_refetched_snapshot_that_differs_is_refused(
    plan_dir, record_calls, monkeypatch
):
    """Refetched content that is not byte-identical is refused."""
    digest = digest_of(TEXT)
    monkeypatch.setattr(
        ctxsrc, 'resolve_dir_source', lambda source: _fake_source(OTHER_TEXT)
    )

    with pytest.raises(ctxsrc.ContextSourceError) as excinfo:
        dispatch_context._load_recorded_context(SOURCE, _recorded(digest))

    assert str(excinfo.value) == (
        f"source {SOURCE['name']!r}: context no longer matches recorded digest {digest}"
    )


def test_a_digest_that_is_not_alphanumeric_hex_is_refused(
    plan_dir, record_calls, monkeypatch
):
    """A digest that is not hex is refused and never used as a path fragment."""
    malicious = 'sha256:../escape'
    monkeypatch.setattr(ctxsrc, 'resolve_dir_source', lambda source: _fake_source(TEXT))

    with pytest.raises((ctxsrc.ContextSourceError, InvalidRecordKeyError)):
        dispatch_context._load_recorded_context(SOURCE, _recorded(malicious, OTHER_TEXT))

    assert not (plan_dir / 'escape').exists()
    assert not (plan_dir.parent / 'escape').exists()
