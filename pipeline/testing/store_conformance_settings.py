"""The settings cases of the published record-store contract (ADR 0015).

Settings is the one record kind added after the rest of the suite, so its cases
live here, beside the suite, rather than in ``store_conformance`` itself, which
sits at the 1000-line gate. ``store_conformance`` imports this module as its
last statement and so re-exports these cases: a store that star-imports the
suite runs them, exactly as it runs the rest.

Imported through ``pipeline.testing.store_conformance``, never on its own: the
``covers`` registry, the ``backend`` fixture and the namespace checks are
defined by that module, and this module needs them already in place.
"""

import pytest

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)
from pipeline.store_settings import SettingsVersionConflictError
from pipeline.testing.store_conformance import (
    FOREIGN,
    INVALID_NAMESPACE,
    Kind,
    check_foreign_read_is_contained,
    check_foreign_write_is_contained,
    check_foreign_write_leaves_default_unchanged,
    check_invalid_namespace_is_refused,
    covers,
)

SETTINGS_SCOPE = 'org'
SETTINGS_SCOPE_ID = 'acme'
SETTINGS_REPO = ''
SETTINGS = {'max_concurrent_agents': 2}
OTHER_SETTINGS = {'max_concurrent_agents': 4}
OPERATOR = 'operator'


def _write_settings(store, namespace, values):
    """Write the settings key's record as ``values``, creating or advancing it.

    The write names the version it read, so the same helper serves the first
    write (a create) and every later one, without a conflict either way.
    """
    current = store.get_settings(
        namespace, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO
    )
    expected = None if current is None else current['version']
    store.put_settings(
        namespace, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO,
        values, [], OPERATOR, expected,
    )


def _read_settings(store, namespace):
    """Return the default settings key's record in ``namespace``, or ``None``."""
    return store.get_settings(namespace, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)


#: The settings kind as the namespace checks see a record kind. It is not in
#: ``KINDS``: that tuple is built before this module loads, and the checks
#: take one kind at a time, so the cases below pass it explicitly.
SETTINGS_KIND = Kind(
    'settings', _write_settings, _read_settings, None, SETTINGS, OTHER_SETTINGS
)


# -- settings (ADR 0015) ---------------------------------------------------------

SETTINGS_REPO_KEY = 'github.com/acme/app'
_SETTINGS_RECORD_FIELDS = {
    'scope', 'scope_id', 'repo', 'values', 'allow_loosen', 'version',
    'updated_at', 'updated_by',
}


def _put_settings(
    store,
    namespace=DEFAULT_NAMESPACE,
    scope=SETTINGS_SCOPE,
    scope_id=SETTINGS_SCOPE_ID,
    repo=SETTINGS_REPO,
    values=SETTINGS,
    allow_loosen=None,
    updated_by=OPERATOR,
    expected_version=None,
):
    return store.put_settings(
        namespace, scope, scope_id, repo, values,
        [] if allow_loosen is None else allow_loosen,
        updated_by, expected_version,
    )


@covers('get_settings', 'put_settings')
def test_settings_round_trip_and_a_missing_record_is_none(backend):
    store = backend.store
    key = (DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)
    assert store.get_settings(*key) is None

    stored = _put_settings(store)
    got = store.get_settings(*key)

    assert set(got) == _SETTINGS_RECORD_FIELDS
    assert got == stored
    assert got['values'] == SETTINGS
    assert got['version'] == 1
    assert got['updated_by'] == OPERATOR
    assert isinstance(got['updated_at'], str)
    assert store.get_settings(*key) == got


@covers('put_settings', 'get_settings')
def test_allow_loosen_is_stored_as_a_sorted_set_of_key_names(backend):
    store = backend.store
    _put_settings(store, allow_loosen=['max_concurrent_agents', 'alpha', 'alpha'])

    got = store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)
    assert got['allow_loosen'] == ['alpha', 'max_concurrent_agents']


@covers('put_settings', 'get_settings')
def test_each_settings_write_advances_the_version_by_one(backend):
    store = backend.store
    assert _put_settings(store, values=SETTINGS)['version'] == 1
    assert _put_settings(store, values=OTHER_SETTINGS, expected_version=1)['version'] == 2

    current = store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)
    assert current['version'] == 2
    assert current['values'] == OTHER_SETTINGS


@covers('put_settings', 'get_settings')
def test_a_stale_settings_version_is_refused_and_the_record_is_unchanged(backend):
    store = backend.store
    _put_settings(store, values=SETTINGS)
    _put_settings(store, values=OTHER_SETTINGS, expected_version=1)

    with pytest.raises(SettingsVersionConflictError):
        _put_settings(store, values=SETTINGS, expected_version=1)

    current = store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)
    assert current['version'] == 2
    assert current['values'] == OTHER_SETTINGS


@covers('put_settings', 'get_settings')
def test_a_second_settings_create_is_refused_and_the_first_record_is_kept(backend):
    store = backend.store
    _put_settings(store, values=SETTINGS)

    with pytest.raises(SettingsVersionConflictError):
        _put_settings(store, values=OTHER_SETTINGS)

    current = store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO)
    assert current['version'] == 1
    assert current['values'] == SETTINGS


@covers('put_settings', 'get_settings')
def test_an_expected_version_on_a_missing_settings_record_is_refused(backend):
    store = backend.store

    with pytest.raises(SettingsVersionConflictError):
        _put_settings(store, expected_version=1)

    assert store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO) is None


@covers('put_settings', 'get_settings')
def test_settings_records_are_kept_per_key(backend):
    store = backend.store
    _put_settings(store, scope='deployment', scope_id='', values=SETTINGS)
    _put_settings(store, scope='org', scope_id='acme', values=OTHER_SETTINGS)

    deployment = store.get_settings(DEFAULT_NAMESPACE, 'deployment', '', SETTINGS_REPO)
    org = store.get_settings(DEFAULT_NAMESPACE, 'org', 'acme', SETTINGS_REPO)
    assert deployment['values'] == SETTINGS
    assert org['values'] == OTHER_SETTINGS


_INVALID_SETTINGS_WRITES = (
    pytest.param({'scope': 'galaxy'}, InvalidRecordKeyError, id='unknown-scope'),
    pytest.param({'scope': 'deployment', 'scope_id': 'acme'}, InvalidRecordKeyError,
                 id='deployment-with-a-scope-id'),
    pytest.param({'scope_id': ''}, InvalidRecordKeyError, id='org-without-a-scope-id'),
    pytest.param({'scope_id': 'Acme Corp'}, InvalidRecordKeyError,
                 id='scope-id-not-an-identifier'),
    pytest.param({'repo': SETTINGS_REPO_KEY}, InvalidRecordKeyError,
                 id='repo-on-a-scope-without-one'),
    pytest.param({'scope': 'team_repo', 'scope_id': 'alpha', 'repo': 'not-a-repo'},
                 InvalidRecordKeyError, id='malformed-repo'),
    pytest.param({'scope': 'team_repo', 'scope_id': 'alpha',
                  'repo': 'github.com/acme/' + 'a' * 250},
                 InvalidRecordKeyError, id='repo-over-256-characters'),
    pytest.param({'values': ['not', 'a', 'dict']}, TypeError, id='values-not-a-dict'),
    pytest.param({'values': {'max_concurrent_agents': object()}}, TypeError,
                 id='values-not-json-serialisable'),
    pytest.param({'allow_loosen': 'max_concurrent_agents'}, TypeError,
                 id='allow-loosen-not-a-list'),
    pytest.param({'updated_by': None}, TypeError, id='updated-by-not-a-str'),
    pytest.param({'expected_version': '1'}, TypeError, id='version-not-an-int'),
    pytest.param({'expected_version': True}, TypeError, id='version-is-a-bool'),
)


@pytest.mark.parametrize('overrides, error', _INVALID_SETTINGS_WRITES)
@covers('put_settings', 'get_settings', 'list_settings')
def test_a_settings_write_with_an_invalid_key_or_shape_is_refused_and_nothing_is_written(
    backend, overrides, error
):
    store = backend.store
    with pytest.raises(error):
        _put_settings(store, **overrides)

    assert store.get_settings(DEFAULT_NAMESPACE, SETTINGS_SCOPE, SETTINGS_SCOPE_ID, SETTINGS_REPO) is None
    assert store.list_settings(DEFAULT_NAMESPACE) == []


@covers('put_settings', 'list_settings')
def test_settings_list_is_sorted_by_key_and_refuses_other_namespaces(backend):
    store = backend.store
    _put_settings(store, scope='team', scope_id='alpha', values=SETTINGS)
    _put_settings(store, scope='deployment', scope_id='', values=OTHER_SETTINGS)
    _put_settings(store, scope='team_repo', scope_id='alpha', repo=SETTINGS_REPO_KEY,
                  values=SETTINGS)

    listed = store.list_settings(DEFAULT_NAMESPACE)
    assert [(r['scope'], r['scope_id'], r['repo']) for r in listed] == [
        ('deployment', '', ''),
        ('team', 'alpha', ''),
        ('team_repo', 'alpha', SETTINGS_REPO_KEY),
    ]
    assert [r['values'] for r in listed] == [OTHER_SETTINGS, SETTINGS, SETTINGS]

    with pytest.raises(InvalidRecordKeyError):
        store.list_settings(INVALID_NAMESPACE)
    if 'settings' in backend.serves_other:
        assert store.list_settings(FOREIGN) == []
    else:
        with pytest.raises(UnknownNamespaceError):
            store.list_settings(FOREIGN)


@covers('get_settings', 'put_settings')
def test_a_malformed_namespace_is_refused_for_the_settings_kind(backend):
    check_invalid_namespace_is_refused(backend.store, SETTINGS_KIND)


@covers('get_settings')
def test_a_foreign_namespace_reads_no_default_settings_record(backend):
    check_foreign_read_is_contained(
        backend.store, 'settings' in backend.serves_other, SETTINGS_KIND
    )


@covers('put_settings', 'get_settings')
def test_a_foreign_settings_write_never_reaches_the_default_namespace(backend):
    check_foreign_write_is_contained(
        backend.store, 'settings' in backend.serves_other, SETTINGS_KIND
    )


@covers('put_settings', 'get_settings')
def test_a_foreign_settings_write_leaves_the_default_record_unchanged(backend):
    check_foreign_write_leaves_default_unchanged(
        backend.store, 'settings' in backend.serves_other, SETTINGS_KIND
    )
