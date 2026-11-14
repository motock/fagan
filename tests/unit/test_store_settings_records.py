"""Contract tests for the ``settings`` record kind (ADR 0015 decision 5).

Every test runs against both the file-layout records view and the in-memory
store, so the two implementations are held to the same rules.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pipeline import persistence, server
from pipeline.store import FileStore
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
    UnreadableRecordError,
)
from pipeline.store_settings import SettingsVersionConflictError

_OTHER_NAMESPACE = 'org-1'
_REPO = 'github.com/acme/app'
_VALUES = {'merge_ci_gate': True}
_LATER_VALUES = {'merge_ci_gate': False, 'merge_ci_timeout_s': 600}


@pytest.fixture(autouse=True)
def plan_dir(tmp_path, monkeypatch):
    d = tmp_path / 'plans'
    d.mkdir()
    monkeypatch.setattr(server, 'PLAN_DIR', d)
    monkeypatch.setattr(persistence, 'PLAN_DIR', d)
    return d


@pytest.fixture(params=['file', 'memory'])
def records(request):
    if request.param == 'file':
        return FileStore().records()
    return InMemoryRecordStore()


def _put(records, scope='deployment', scope_id='', repo='', values=None,
         allow_loosen=None, expected_version=None, updated_by='ops'):
    return records.put_settings(
        DEFAULT_NAMESPACE,
        scope,
        scope_id,
        repo,
        _VALUES if values is None else values,
        [] if allow_loosen is None else allow_loosen,
        updated_by,
        expected_version,
    )


# -- round trip and versioning ----------------------------------------


def test_put_then_get_round_trips_the_settings_record(records):
    stored = _put(records, scope='org', scope_id='acme', allow_loosen=['merge_ci_gate'])

    got = records.get_settings(DEFAULT_NAMESPACE, 'org', 'acme', '')

    assert got == stored
    assert got['scope'] == 'org'
    assert got['scope_id'] == 'acme'
    assert got['repo'] == ''
    assert got['values'] == _VALUES
    assert got['allow_loosen'] == ['merge_ci_gate']
    assert got['version'] == 1
    assert got['updated_by'] == 'ops'


def test_get_of_an_unset_record_returns_none(records):
    assert records.get_settings(DEFAULT_NAMESPACE, 'team', 'alpha', '') is None


def test_updated_at_is_an_iso_8601_timestamp(records):
    stored = _put(records)

    assert datetime.fromisoformat(stored['updated_at']).tzinfo is not None


def test_each_successful_put_increments_the_version(records):
    _put(records)
    second = _put(records, values=_LATER_VALUES, expected_version=1)
    third = _put(records, values=_VALUES, expected_version=2)

    assert second['version'] == 2
    assert third['version'] == 3
    assert records.get_settings(DEFAULT_NAMESPACE, 'deployment', '', '')['values'] == _VALUES


def test_a_stale_expected_version_is_refused_and_the_record_is_unchanged(records):
    _put(records)
    _put(records, values=_LATER_VALUES, expected_version=1)

    with pytest.raises(SettingsVersionConflictError):
        _put(records, values={'merge_ci_gate': True}, expected_version=1)

    got = records.get_settings(DEFAULT_NAMESPACE, 'deployment', '', '')
    assert got['version'] == 2
    assert got['values'] == _LATER_VALUES


def test_a_create_twice_is_refused_and_the_first_record_is_kept(records):
    _put(records, values=_VALUES)

    with pytest.raises(SettingsVersionConflictError):
        _put(records, values=_LATER_VALUES, expected_version=None)

    got = records.get_settings(DEFAULT_NAMESPACE, 'deployment', '', '')
    assert got['values'] == _VALUES
    assert got['version'] == 1


def test_an_expected_version_on_a_missing_record_is_refused(records):
    with pytest.raises(SettingsVersionConflictError):
        _put(records, scope='org', scope_id='acme', expected_version=1)

    assert records.get_settings(DEFAULT_NAMESPACE, 'org', 'acme', '') is None


def test_a_non_int_expected_version_is_refused(records):
    _put(records)

    with pytest.raises(TypeError):
        _put(records, expected_version='1')


# -- namespace --------------------------------------------------------


def test_a_foreign_namespace_is_refused_for_get_put_and_list(records):
    with pytest.raises(UnknownNamespaceError):
        records.get_settings(_OTHER_NAMESPACE, 'deployment', '', '')
    with pytest.raises(UnknownNamespaceError):
        records.put_settings(_OTHER_NAMESPACE, 'deployment', '', '', _VALUES, [], 'ops', None)
    with pytest.raises(UnknownNamespaceError):
        records.list_settings(_OTHER_NAMESPACE)

    assert records.get_settings(DEFAULT_NAMESPACE, 'deployment', '', '') is None


def test_an_invalid_namespace_name_is_refused(records):
    with pytest.raises(InvalidRecordKeyError):
        records.list_settings('Bad Namespace')


# -- key shape --------------------------------------------------------


@pytest.mark.parametrize(
    ('scope', 'scope_id', 'repo'),
    [
        ('', '', ''),
        ('global', '', ''),
        ('Deployment', '', ''),
        ('deployment', 'acme', ''),
        ('org', '', ''),
        ('org', 'Acme Corp', ''),
        ('org', 'acme', _REPO),
        ('team', '', ''),
        ('team', '../alpha', ''),
        ('team', 'alpha', _REPO),
        ('team_repo', 'alpha', ''),
        ('team_repo', '', _REPO),
        ('team_repo', 'alpha', 'x' * 257),
        ('team_repo', 'alpha', 'github.com/acme/app\n'),
        ('team_repo', 'alpha', 'github.com/acme/\x7fapp'),
        ('team_repo', 'alpha', 'github.com/acme'),
        ('team_repo', 'alpha', 'github.com/acme/app/extra'),
    ],
)
def test_an_invalid_key_shape_is_refused_and_nothing_is_written(
    records, scope, scope_id, repo
):
    with pytest.raises(InvalidRecordKeyError):
        _put(records, scope=scope, scope_id=scope_id, repo=repo)

    assert records.list_settings(DEFAULT_NAMESPACE) == []


def test_a_repo_of_exactly_256_characters_is_accepted(records):
    repo = 'github.com/acme/' + 'a' * (256 - len('github.com/acme/'))

    stored = _put(records, scope='team_repo', scope_id='alpha', repo=repo)

    assert stored['repo'] == repo


def test_a_non_dict_values_is_refused_and_nothing_is_written(records):
    with pytest.raises(TypeError):
        _put(records, values=['merge_ci_gate'])

    assert records.list_settings(DEFAULT_NAMESPACE) == []


def test_allow_loosen_must_be_a_list_of_key_names(records):
    with pytest.raises(TypeError):
        _put(records, allow_loosen=[1])


# -- listing ----------------------------------------------------------


def test_list_settings_of_an_empty_namespace_is_empty(records):
    assert records.list_settings(DEFAULT_NAMESPACE) == []


def test_list_settings_returns_every_record_in_the_namespace(records):
    _put(records)
    _put(records, scope='org', scope_id='acme')
    _put(records, scope='team', scope_id='alpha')

    listed = records.list_settings(DEFAULT_NAMESPACE)

    assert sorted((r['scope'], r['scope_id'], r['repo']) for r in listed) == [
        ('deployment', '', ''),
        ('org', 'acme', ''),
        ('team', 'alpha', ''),
    ]


# -- team-x-repo keys -------------------------------------------------


def test_two_teams_on_one_repository_keep_separate_records(records):
    _put(records, scope='team_repo', scope_id='alpha', repo=_REPO, values={'merge_ci_timeout_s': 300})
    _put(records, scope='team_repo', scope_id='beta', repo=_REPO, values={'merge_ci_timeout_s': 900})

    alpha = records.get_settings(DEFAULT_NAMESPACE, 'team_repo', 'alpha', _REPO)
    beta = records.get_settings(DEFAULT_NAMESPACE, 'team_repo', 'beta', _REPO)

    assert alpha['values'] == {'merge_ci_timeout_s': 300}
    assert beta['values'] == {'merge_ci_timeout_s': 900}
    assert alpha['version'] == 1
    assert beta['version'] == 1
    assert len(records.list_settings(DEFAULT_NAMESPACE)) == 2


# -- file layout ------------------------------------------------------


def _file_records(plan_dir):
    return FileStore().records()


def test_a_malformed_settings_file_is_unreadable_not_absent(plan_dir):
    records = _file_records(plan_dir)
    _put(records)
    (path,) = plan_dir.glob('settings-record-*.json')
    path.write_text('{not json', encoding='utf-8')

    with pytest.raises(UnreadableRecordError):
        records.get_settings(DEFAULT_NAMESPACE, 'deployment', '', '')
    with pytest.raises(UnreadableRecordError):
        records.list_settings(DEFAULT_NAMESPACE)


def test_a_settings_file_renamed_to_another_key_is_refused(plan_dir):
    records = _file_records(plan_dir)
    _put(records)
    (path,) = plan_dir.glob('settings-record-*.json')
    path.rename(plan_dir / f'settings-record-{"0" * 64}.json')

    with pytest.raises(UnreadableRecordError):
        records.list_settings(DEFAULT_NAMESPACE)


def test_a_refused_write_writes_no_record_and_no_temp_file(plan_dir):
    records = _file_records(plan_dir)

    with pytest.raises(InvalidRecordKeyError):
        _put(records, scope='team_repo', scope_id='alpha', repo='')
    with pytest.raises(SettingsVersionConflictError):
        _put(records, expected_version=3)

    assert list(plan_dir.glob('settings-record-*')) == []
    assert list(plan_dir.glob('.settings-record-*.tmp')) == []
