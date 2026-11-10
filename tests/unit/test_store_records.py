"""Contract tests for the namespaced record store API (ADR 0012, stage A)."""

from __future__ import annotations

import inspect

import pytest

import pipeline.store_records as store_records  # noqa: PLR0402
from pipeline.notification_channels import (
    ChannelConfigError,
    validate_instance_name,
)


def _attr(name):
    """Return a module attribute, failing the test (not importing) when absent."""
    assert hasattr(store_records, name), f'pipeline.store_records.{name} is missing'
    return getattr(store_records, name)


def test_validate_namespace_accepts_valid_names():
    validate_namespace = _attr('validate_namespace')
    assert validate_namespace('default') == 'default'
    assert validate_namespace('org-1') == 'org-1'


@pytest.mark.parametrize(
    'value',
    ['', 'Default', '../x', 'a b', 'x' * 65, None, 5, b'default'],
)
def test_validate_namespace_rejects_invalid_names(value):
    validate_namespace = _attr('validate_namespace')
    error = _attr('InvalidRecordKeyError')
    with pytest.raises(error):
        validate_namespace(value)


def test_validate_plan_name_accepts_valid_names():
    validate_plan_name = _attr('validate_plan_name')
    for value in ('plugin-seams', 'release-docs-v0-9-0', 'a.b'):
        assert validate_plan_name(value) == value


@pytest.mark.parametrize(
    'value',
    ['', '.hidden', '../etc', 'a/b', 'a..b', 'x' * 129, None],
)
def test_validate_plan_name_rejects_invalid_names(value):
    validate_plan_name = _attr('validate_plan_name')
    error = _attr('InvalidRecordKeyError')
    with pytest.raises(error):
        validate_plan_name(value)


def test_validate_plan_name_rejects_the_reserved_name():
    """The audit anchor log keys the host chain as "host", so a plan may not
    use that name: the record store refuses it before touching storage."""
    validate_plan_name = _attr('validate_plan_name')
    error = _attr('InvalidRecordKeyError')
    with pytest.raises(error) as excinfo:
        validate_plan_name('host')
    assert str(excinfo.value) == 'reserved plan name'


def test_validate_plan_name_accepts_names_near_the_reserved_one():
    """The reservation is exact and case-sensitive: near names stay valid."""
    validate_plan_name = _attr('validate_plan_name')
    for value in ('Host', 'hostname', 'host-1', 'my-host'):
        assert validate_plan_name(value) == value


def test_validate_story_key_accepts_valid_keys():
    validate_story_key = _attr('validate_story_key')
    uuid = '3f2504e0-4f89-11d3-9a0c-0305e82c3301'
    assert validate_story_key(uuid) == uuid
    assert validate_story_key('SN-01') == 'SN-01'


@pytest.mark.parametrize('value', ['', 'a/b', '..', None])
def test_validate_story_key_rejects_invalid_keys(value):
    validate_story_key = _attr('validate_story_key')
    error = _attr('InvalidRecordKeyError')
    with pytest.raises(error):
        validate_story_key(value)


@pytest.mark.parametrize(
    'validator_name',
    ['validate_namespace', 'validate_plan_name', 'validate_story_key'],
)
def test_error_text_never_echoes_the_offending_value(validator_name):
    validator = _attr(validator_name)
    error = _attr('InvalidRecordKeyError')
    offending = '../secret-path'
    with pytest.raises(error) as excinfo:
        validator(offending)
    assert offending not in str(excinfo.value)


def test_error_hierarchy():
    store_error = _attr('RecordStoreError')
    invalid_key_error = _attr('InvalidRecordKeyError')
    unknown_namespace_error = _attr('UnknownNamespaceError')
    assert issubclass(invalid_key_error, store_error)
    assert issubclass(invalid_key_error, ValueError)
    assert issubclass(unknown_namespace_error, store_error)
    assert issubclass(unknown_namespace_error, LookupError)


def test_record_store_methods_declare_namespace_first():
    protocol = _attr('RecordStore')
    declared = {
        name
        for name, value in protocol.__dict__.items()
        if not name.startswith('_') and callable(value)
    }
    assert set(_attr('RECORD_STORE_METHODS')) == declared
    for name in sorted(declared):
        parameters = list(inspect.signature(getattr(protocol, name)).parameters)
        assert parameters[0] == 'self', f'{name} must take self first'
        assert parameters[1] == 'namespace', f'{name} must take namespace second'


def test_default_namespace_is_valid():
    validate_namespace = _attr('validate_namespace')
    default_namespace = _attr('DEFAULT_NAMESPACE')
    assert default_namespace == 'default'
    assert validate_namespace(default_namespace) == default_namespace


# -- channels (ADR 0012 decision 3) ---------------------------------
LEGACY_OUTBOX_CHANNEL = '_legacy'


def test_the_legacy_outbox_channel_is_the_underscore_prefixed_key():
    assert _attr('LEGACY_OUTBOX_CHANNEL') == LEGACY_OUTBOX_CHANNEL


def test_validate_channel_accepts_the_legacy_channel_and_instance_names():
    validate_channel = _attr('validate_channel')
    for value in (LEGACY_OUTBOX_CHANNEL, 'email-a', 'legacy', 'a', 'a' * 32):
        assert validate_channel(value) == value


@pytest.mark.parametrize(
    'value',
    ['Bad', '', '.x', 'a' * 33, 'a/b', '_legacy-2', 'a b'],
)
def test_validate_channel_rejects_invalid_channel_names(value):
    validate_channel = _attr('validate_channel')
    error = _attr('InvalidRecordKeyError')
    with pytest.raises(error):
        validate_channel(value)


def test_validate_channel_error_text_never_echoes_the_offending_value():
    validate_channel = _attr('validate_channel')
    error = _attr('InvalidRecordKeyError')
    offending = '../secret-channel'
    with pytest.raises(error) as excinfo:
        validate_channel(offending)
    assert offending not in str(excinfo.value)
    assert 'invalid channel' in str(excinfo.value)


def test_the_legacy_channel_cannot_be_an_instance_name():
    """The legacy key starts with ``_``, so an instance can never be it."""
    legacy = _attr('LEGACY_OUTBOX_CHANNEL')
    with pytest.raises(ChannelConfigError):
        validate_instance_name(legacy)
    # An operator instance called 'legacy' is fine and never collides with it.
    validate_instance_name('legacy')


def test_the_outbox_contract_module_exposes_the_outbox_mixin():
    """``pipeline.store_contract_outbox`` carries the shared ``OutboxContract``."""
    import pipeline.store_contract_outbox as contract

    assert hasattr(contract, 'OutboxContract')
    assert isinstance(contract.OutboxContract, type)
