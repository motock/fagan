"""Contract tests for the context-snapshot records (ADR 0012 decision 11).

A context snapshot is an immutable, content-addressed blob: the digest is the
SHA-256 of the content, the snapshot is written once and never overwritten,
and there is no delete and no listing -- only ``put`` and ``get``.

The two record operations are ``put_context_snapshot(namespace, digest,
content, meta)`` and ``get_context_snapshot(namespace, digest)``, and they are
asserted against the three record-store implementations: the file-backed store
(``FileRecords``, which keeps today's ``PLAN_DIR/context/<hex>/content.txt`` +
``meta.json`` layout), the sqlite store (``SqliteRecords``) and the in-memory
reference store (``InMemoryRecordStore``).

Every backend answers the same questions, so the tests are shared:

* a round trip returns the very content and meta that were put;
* a digest that does not match the content is refused and nothing is written;
* a malformed digest (no ``sha256:`` prefix, or non-hex characters) is refused
  with ``InvalidRecordKeyError`` and is never used as a path fragment;
* a second put of an existing digest is a no-op that keeps the first meta;
* an absent snapshot reads back as ``None``;
* an unknown namespace is refused;
* empty content is allowed (the digest of the empty string).

The contract itself is part of the shared record contract, so the Protocol and
the closed method tuple are checked for the two new names as well.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from pipeline import paths, persistence, server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

SNAPSHOT_METHODS = ('put_context_snapshot', 'get_context_snapshot')

CONTENT = '--- guide.md ---\nUSE-TABS-ALWAYS\n'
META = {'provider': 'dir', 'uri': '/srv/docs'}


def digest_of(content: str) -> str:
    """Return the canonical digest of *content*."""
    return 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()


@pytest.fixture(params=('file', 'sqlite', 'memory'))
def backend(request):
    """The implementation under test, one per run of every test below."""
    return request.param


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """An empty plans directory wired into every module-level binding.

    ``FileRecords`` keeps today's layout under the plans directory and
    ``pipeline.paths.PLAN_DIR`` is the binding the context code reads, so both
    are pointed at the same directory: the file layout is then where a caller
    expects it, whichever binding the implementation reads.
    """
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    monkeypatch.setattr(paths, 'PLAN_DIR', d)
    return d


@pytest.fixture
def store(plan_dir, tmp_path, backend):
    """The record view of the implementation under test."""
    if backend == 'memory':
        yield InMemoryRecordStore()
        return
    if backend == 'file':
        yield FileStore().records()
        return
    sqlite_store = SqliteStore(str(tmp_path / 'records.db'))
    try:
        yield sqlite_store.records()
    finally:
        close = getattr(sqlite_store, 'close', None)
        if close is not None:
            close()


# -- the contract itself ------------------------------------------------


def test_record_store_methods_contain_the_context_snapshot_names():
    """The two context-snapshot operations are part of the closed contract."""
    from pipeline import store_records

    for name in SNAPSHOT_METHODS:
        assert name in store_records.RECORD_STORE_METHODS, (
            f'{name} must be in RECORD_STORE_METHODS'
        )


def test_the_record_store_protocol_declares_the_context_snapshot_names():
    """The Protocol declares the same two names as the method tuple."""
    from pipeline import store_records

    for name in SNAPSHOT_METHODS:
        assert hasattr(store_records.RecordStore, name), (
            f'RecordStore must declare {name}'
        )


def test_there_is_no_delete_and_no_listing(store):
    """A snapshot is immutable: there is no delete and no listing."""
    for name in ('delete_context_snapshot', 'list_context_snapshots'):
        assert not hasattr(store, name), f'{name} must not exist'


# -- the round trip -----------------------------------------------------


def test_should_read_back_the_content_and_the_meta_that_were_put(store):
    """A put is readable and returns exactly what was stored."""
    digest = digest_of(CONTENT)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, META)

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == CONTENT
    assert found['meta']['provider'] == 'dir'
    assert found['meta']['uri'] == '/srv/docs'


def test_should_return_none_for_an_absent_snapshot(store):
    """A snapshot that was never put reads back as ``None``."""
    assert store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT)) is None


def test_should_return_none_for_the_digest_of_empty_content_when_absent(store):
    """The digest of the empty string is a valid key that simply is not there."""
    assert store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of('')) is None


def test_should_store_empty_content(store):
    """Empty content is allowed: its digest is the digest of ``''``."""
    digest = digest_of('')
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, '', {})

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == ''


def test_should_keep_two_snapshots_apart(store):
    """Snapshots are keyed by their digest, so two contents live side by side."""
    other = '--- other.md ---\nOTHER\n'
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT), CONTENT, META)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest_of(other), other, {})

    assert store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(CONTENT))['content'] == CONTENT
    assert store.get_context_snapshot(DEFAULT_NAMESPACE, digest_of(other))['content'] == other


# -- the digest is the address -----------------------------------------


def test_should_refuse_a_digest_that_does_not_match_the_content(store):
    """A digest that is not the SHA-256 of the content is refused."""
    wrong = digest_of('something else entirely')

    with pytest.raises(ValueError):
        store.put_context_snapshot(DEFAULT_NAMESPACE, wrong, CONTENT, META)

    assert store.get_context_snapshot(DEFAULT_NAMESPACE, wrong) is None


def test_should_write_nothing_when_the_digest_does_not_match(store, plan_dir):
    """A refused put leaves no snapshot behind."""
    wrong = digest_of('something else entirely')

    with pytest.raises(ValueError):
        store.put_context_snapshot(DEFAULT_NAMESPACE, wrong, CONTENT, META)

    assert not list(plan_dir.rglob('content.txt'))
    assert not list(plan_dir.rglob('meta.json'))


def test_should_refuse_a_digest_without_the_sha256_prefix(store):
    """A bare hex digest is not a digest: the prefix is part of the key."""
    bare = hashlib.sha256(CONTENT.encode('utf-8')).hexdigest()

    with pytest.raises(InvalidRecordKeyError):
        store.put_context_snapshot(DEFAULT_NAMESPACE, bare, CONTENT, META)

    assert store.get_context_snapshot(DEFAULT_NAMESPACE, bare) is None


def test_should_refuse_a_digest_with_non_hex_characters(store):
    """A digest whose hex part is not hex is refused."""
    malformed = 'sha256:' + 'z' * 64

    with pytest.raises(InvalidRecordKeyError):
        store.put_context_snapshot(DEFAULT_NAMESPACE, malformed, CONTENT, META)

    assert store.get_context_snapshot(DEFAULT_NAMESPACE, malformed) is None


def test_should_refuse_a_digest_that_is_a_path_fragment(store, plan_dir, tmp_path):
    """A digest is never used as a path fragment, so it cannot escape."""
    outside = tmp_path / 'escaped'
    malicious = f'sha256:{outside}'

    with pytest.raises(InvalidRecordKeyError):
        store.put_context_snapshot(DEFAULT_NAMESPACE, malicious, CONTENT, META)

    assert not outside.exists()
    assert not list(plan_dir.rglob('content.txt'))


def test_should_refuse_to_read_a_malformed_digest(store):
    """Reading with a malformed digest is refused, not attempted."""
    with pytest.raises(InvalidRecordKeyError):
        store.get_context_snapshot(DEFAULT_NAMESPACE, 'sha256:not-a-digest')


# -- immutability -------------------------------------------------------


def test_a_second_put_keeps_the_first_meta(store):
    """A second put of an existing digest never overwrites the snapshot."""
    digest = digest_of(CONTENT)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, {'first': 1})
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, {'second': 2})

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == CONTENT
    assert found['meta'].get('first') == 1
    assert 'second' not in found['meta']


def test_a_second_put_keeps_the_first_content(store):
    """The stored content of an existing digest is never replaced."""
    digest = digest_of(CONTENT)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, {})
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, {})

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == CONTENT


# -- namespaces ---------------------------------------------------------


def test_should_refuse_an_invalid_namespace(store):
    """A namespace that is not a valid key is refused before anything is written."""
    with pytest.raises(InvalidRecordKeyError):
        store.put_context_snapshot('Not A Namespace', digest_of(CONTENT), CONTENT, META)

    with pytest.raises(InvalidRecordKeyError):
        store.get_context_snapshot('Not A Namespace', digest_of(CONTENT))


@pytest.mark.parametrize('backend', ('file', 'sqlite'))
def test_should_refuse_an_unknown_namespace(backend, plan_dir, tmp_path):
    """The file and sqlite stores serve the default namespace only."""
    if backend == 'file':
        records = FileStore().records()
    else:
        records = SqliteStore(str(tmp_path / 'records.db')).records()

    with pytest.raises(UnknownNamespaceError):
        records.put_context_snapshot('other', digest_of(CONTENT), CONTENT, META)

    with pytest.raises(UnknownNamespaceError):
        records.get_context_snapshot('other', digest_of(CONTENT))


# -- the file layout ----------------------------------------------------


def test_the_file_store_keeps_todays_layout(store, plan_dir, backend):
    """``FileRecords`` keeps ``PLAN_DIR/context/<hex>/content.txt`` + ``meta.json``."""
    if backend != 'file':
        pytest.skip('the file layout is the file store\'s own')

    digest = digest_of(CONTENT)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, META)

    folder = plan_dir / 'context' / digest.removeprefix('sha256:')
    assert (folder / 'content.txt').read_text(encoding='utf-8') == CONTENT
    assert json.loads((folder / 'meta.json').read_text(encoding='utf-8'))['provider'] == 'dir'


def test_the_file_store_reads_a_snapshot_written_by_the_layout(store, plan_dir, backend):
    """A snapshot written as today's layout is readable through the record view."""
    if backend != 'file':
        pytest.skip('the file layout is the file store\'s own')

    digest = digest_of(CONTENT)
    folder = plan_dir / 'context' / digest.removeprefix('sha256:')
    folder.mkdir(parents=True)
    (folder / 'content.txt').write_text(CONTENT, encoding='utf-8')
    (folder / 'meta.json').write_text(
        json.dumps({**META, 'digest': digest, 'bytes': len(CONTENT.encode('utf-8'))}),
        encoding='utf-8',
    )

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)

    assert found is not None
    assert found['content'] == CONTENT
    assert found['meta']['provider'] == 'dir'


# -- the sqlite backend keeps the snapshot in its database ----------------


def test_the_sqlite_backend_keeps_the_snapshot_in_its_database_not_on_disk(
    store, plan_dir, backend
):
    """The sqlite store's snapshot is a row, so the plans directory stays empty.

    The file backend still keeps today's layout; the sqlite backend moved the
    snapshot into its own database, so a snapshot it stored leaves no file
    behind.
    """
    if backend != 'sqlite':
        pytest.skip('the database layout is the sqlite store\'s own')

    digest = digest_of(CONTENT)
    store.put_context_snapshot(DEFAULT_NAMESPACE, digest, CONTENT, META)

    found = store.get_context_snapshot(DEFAULT_NAMESPACE, digest)
    assert found is not None
    assert found['content'] == CONTENT
    assert list(plan_dir.iterdir()) == []
