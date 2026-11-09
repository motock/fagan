"""Reusable behaviour suite for ``RecordStore`` usage records (ADR 0012 decision 3).

Decision 3 makes usage an append-only accounting artefact: every record store
offers exactly ``append_usage`` and ``get_usage`` for it and nothing else -- no
update, no delete -- and a record whose key set is not exactly the contract's
field set, or whose values are not values those fields may hold, is refused
before anything is written.

Like :mod:`pipeline.store_contract`, this module imports no test framework: it
uses plain ``assert`` statements and the small helpers below, so the same cases
run under any harness.  None of the classes is named ``Test*`` -- pytest must
never collect a mixin (it would call the unimplemented ``make_store``).

Inherit :class:`UsageContract` in a concrete test class alongside the behaviour
mixins from :mod:`pipeline.store_contract`.
"""

from __future__ import annotations

import math

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    USAGE_RECORD_FIELDS,
    InvalidUsageRecordError,
)

_PLAN = 'plan-a'
_OTHER_PLAN = 'plan-b'

#: The worked example from ``docs/TELEMETRY_CONTRACT.md`` "Usage record",
#: attributed to the contract's test plan name instead of ``default``.
WORKED_EXAMPLE = {
    'ts': '2026-11-05T10:12:33.412Z',
    'plan': _PLAN,
    'story_key': 'STORY-42',
    'attempt': 2,
    'correlation_id': '0f9c4b7e',
    'role': 'dispatch',
    'backend': 'claude',
    'model': 'claude-sonnet-4-5',
    'served_model': 'claude-sonnet-4-5-20250929',
    'input_tokens': 1200,
    'output_tokens': 350,
    'cache_creation_input_tokens': 0,
    'cache_read_input_tokens': 800,
    'cost_usd': 0.0042,
    'duration_ms': 2340,
}


def usage_record(plan_name: str = _PLAN, **overrides) -> dict:
    """Return the worked example attributed to ``plan_name``, with overrides."""
    record = dict(WORKED_EXAMPLE)
    record['plan'] = plan_name
    record.update(overrides)
    return record


def _expect(exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``."""
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f'{fn} did not raise {exc_type.__name__}')


class UsageContract:
    """The usage-record behaviour every ``RecordStore`` must show."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def plans_dir(self):
        """Return the directory a file-backed store appends usage to, or None.

        Used to prove that a rejected record is refused *before* anything is
        written: no ``<plan>.usage.jsonl`` may appear.  A store with no such
        file returns ``None`` and that assertion is skipped.
        """

    # -- helpers -------------------------------------------------------
    def _assert_rejected(self, store, record, exc_type=InvalidUsageRecordError):
        """Assert ``record`` is refused and that the stored history is unchanged."""
        before = store.get_usage(DEFAULT_NAMESPACE, _PLAN)
        _expect(exc_type, lambda: store.append_usage(DEFAULT_NAMESPACE, _PLAN, record))
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == before

    # -- the record round-trips ----------------------------------------
    def test_usage_of_a_never_written_plan_is_empty(self):
        store = self.make_store()
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == []

    def test_worked_example_round_trips_equal(self):
        store = self.make_store()
        record = usage_record()
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == [record]

    def test_records_keep_the_order_they_were_appended(self):
        store = self.make_store()
        records = [
            usage_record(ts=f'2026-11-05T10:12:3{index}.000Z', story_key=f'STORY-{index}')
            for index in range(3)
        ]
        for record in records:
            store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == records

    def test_zero_tokens_and_zero_cost_are_accepted(self):
        """A cached call costing nothing is accounted for, not rejected."""
        store = self.make_store()
        record = usage_record(
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cost_usd=0,
            duration_ms=0,
        )
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == [record]

    def test_served_model_differing_from_model_is_preserved(self):
        store = self.make_store()
        record = usage_record(served_model='claude-sonnet-4-5-20990101')
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        stored = store.get_usage(DEFAULT_NAMESPACE, _PLAN)
        assert stored[0]['served_model'] == 'claude-sonnet-4-5-20990101'
        assert stored[0]['model'] == 'claude-sonnet-4-5'

    def test_optional_ids_may_be_none(self):
        """A call not made for a story, and a provider that serves no model id."""
        store = self.make_store()
        record = usage_record(story_key=None, attempt=None, correlation_id=None, served_model=None)
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        assert store.get_usage(DEFAULT_NAMESPACE, _PLAN) == [record]

    def test_usage_is_per_plan(self):
        store = self.make_store()
        record = usage_record()
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, record)
        assert store.get_usage(DEFAULT_NAMESPACE, _OTHER_PLAN) == []

    # -- the record is validated before anything is written -------------
    def test_append_usage_rejects_an_extra_field(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(prompt='hello'))

    def test_append_usage_rejects_a_missing_field(self):
        store = self.make_store()
        record = usage_record()
        del record['served_model']
        self._assert_rejected(store, record)

    def test_append_usage_rejects_a_plan_that_does_not_match(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(plan=_OTHER_PLAN))

    def test_append_usage_rejects_a_negative_token_count(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(output_tokens=-1))

    def test_append_usage_rejects_a_bool_token_count(self):
        """``isinstance(True, int)`` is true; a flag is not a token count."""
        store = self.make_store()
        self._assert_rejected(store, usage_record(input_tokens=True))

    def test_append_usage_rejects_a_negative_cost(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(cost_usd=-0.01))

    def test_append_usage_rejects_a_nan_cost(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(cost_usd=float('nan')))

    def test_append_usage_rejects_a_non_finite_duration(self):
        store = self.make_store()
        self._assert_rejected(store, usage_record(duration_ms=math.inf))

    def test_append_usage_rejects_a_non_dict_record(self):
        store = self.make_store()
        self._assert_rejected(store, ['ts', 'plan'], exc_type=TypeError)

    def test_a_rejected_record_writes_nothing(self):
        """The refusal happens before storage is touched: no file appears."""
        directory = self.plans_dir()
        if directory is None:
            return
        store = self.make_store()
        _expect(
            InvalidUsageRecordError,
            lambda: store.append_usage(
                DEFAULT_NAMESPACE, _PLAN, usage_record(output_tokens=-1)
            ),
        )
        assert not (directory / f'{_PLAN}.usage.jsonl').exists()

    def test_the_error_names_the_field_and_not_its_value(self):
        store = self.make_store()
        try:
            store.append_usage(DEFAULT_NAMESPACE, _PLAN, usage_record(cost_usd=-1234.5))
        except InvalidUsageRecordError as exc:
            message = str(exc)
        else:  # pragma: no cover - the rejection is the point
            raise AssertionError('a negative cost was accepted')
        assert 'cost_usd' in message
        assert '-1234.5' not in message

    # -- append-only by construction ------------------------------------
    def test_store_offers_no_usage_operation_but_append_and_get(self):
        """No update, no delete: usage cannot be rewritten through the API."""
        store = self.make_store()
        usage_methods = {
            name
            for name in dir(store)
            if 'usage' in name.lower() and not name.startswith('_')
        }
        assert usage_methods == {'append_usage', 'get_usage'}

    def test_usage_records_are_the_contract_field_set(self):
        store = self.make_store()
        store.append_usage(DEFAULT_NAMESPACE, _PLAN, usage_record())
        (stored,) = store.get_usage(DEFAULT_NAMESPACE, _PLAN)
        assert tuple(stored) == USAGE_RECORD_FIELDS
