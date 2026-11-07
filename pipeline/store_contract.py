"""Reusable behaviour suite for :class:`pipeline.store_records.RecordStore`.

ADR 0012 stage A.  A contract suite that only one implementation passes proves
nothing, so the behaviour lives in mixin classes: an implementation inherits the
mixins that describe what it supports and overrides :meth:`make_store`.

This module imports no test framework on purpose.  It uses plain ``assert``
statements and the small :func:`_expect` helper, so the same suite can be
exercised by any harness.  None of the classes is named ``Test*``: pytest must
never collect a mixin (it would call the unimplemented ``make_store``).

Inherit :class:`RecordStoreContract` always, plus exactly one of
:class:`MultiNamespaceContract` (implementations that serve any namespace) or
:class:`SingleNamespaceContract` (implementations that only serve
:data:`DEFAULT_NAMESPACE`).
"""

from __future__ import annotations

from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    InvalidRecordKeyError,
    UnknownNamespaceError,
)

_PLAN = 'plan-a'
_OTHER_PLAN = 'plan-b'
_STORY_KEY = 'STORY-1'
_OTHER_STORY_KEY = 'STORY-2'
_SECOND_NAMESPACE = 'acme'
_INVALID_KEY = '../x'
_INVALID_NAMESPACE = 'Bad NS'


def _expect(self, exc_type, fn):
    """Assert that calling ``fn`` raises exactly ``exc_type``.

    Any other exception propagates and fails the test just as loudly; so does
    returning normally instead of raising.
    """
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f'{fn} did not raise {exc_type.__name__}')


class RecordStoreContract:
    """Default-namespace behaviour plus key and argument validation."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    # -- manifests -----------------------------------------------------
    def test_get_manifest_of_an_absent_plan_is_none(self):
        store = self.make_store()
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) is None

    def test_put_then_get_manifest_returns_an_equal_document(self):
        store = self.make_store()
        manifest = {'title': _PLAN, 'steps': [{'id': 1, 'kind': 'build'}]}
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, manifest)
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == manifest

    def test_mutating_a_returned_manifest_does_not_change_stored_state(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'steps': [{'id': 1}]})
        document = store.get_manifest(DEFAULT_NAMESPACE, _PLAN)
        document['steps'][0]['id'] = 99
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'steps': [{'id': 1}]}

    def test_second_put_manifest_replaces_the_first_document_entirely(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1, 'steps': [{'id': 1}]})
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 2}

    # -- bulk manifest reads -------------------------------------------
    def test_should_return_an_empty_mapping_for_an_empty_store(self):
        store = self.make_store()
        assert store.get_manifests(DEFAULT_NAMESPACE) == {}

    def test_should_return_every_put_manifest_keyed_by_plan_name(self):
        store = self.make_store()
        manifest = {'title': _PLAN, 'steps': [{'id': 1, 'kind': 'build'}]}
        other = {'title': _OTHER_PLAN}
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, manifest)
        store.put_manifest(DEFAULT_NAMESPACE, _OTHER_PLAN, other)
        assert store.get_manifests(DEFAULT_NAMESPACE) == {
            _PLAN: manifest,
            _OTHER_PLAN: other,
        }

    def test_should_return_keys_in_sorted_order(self):
        store = self.make_store()
        for name in ('zeta', 'alpha', 'mike'):
            store.put_manifest(DEFAULT_NAMESPACE, name, {'version': name})
        assert list(store.get_manifests(DEFAULT_NAMESPACE)) == ['alpha', 'mike', 'zeta']

    def test_should_reject_an_invalid_namespace(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.get_manifests(_INVALID_NAMESPACE),
        )
        assert store.get_manifests(DEFAULT_NAMESPACE) == {_PLAN: {'version': 1}}

    # -- plan listing --------------------------------------------------
    def test_list_plans_of_an_empty_store_is_empty(self):
        store = self.make_store()
        assert store.list_plans(DEFAULT_NAMESPACE) == []

    def test_list_plans_returns_the_plan_names_in_sorted_order(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, 'b', {'version': 1})
        store.put_manifest(DEFAULT_NAMESPACE, 'a', {'version': 1})
        assert store.list_plans(DEFAULT_NAMESPACE) == ['a', 'b']

    # -- decisions -----------------------------------------------------
    def test_get_decisions_of_an_unknown_plan_is_empty(self):
        store = self.make_store()
        assert store.get_decisions(DEFAULT_NAMESPACE, _PLAN) == []

    def test_two_appended_decisions_keep_their_order(self):
        store = self.make_store()
        store.append_decision(DEFAULT_NAMESPACE, _PLAN, {'decision': 'first'})
        store.append_decision(DEFAULT_NAMESPACE, _PLAN, {'decision': 'second'})
        assert store.get_decisions(DEFAULT_NAMESPACE, _PLAN) == [
            {'decision': 'first'},
            {'decision': 'second'},
        ]

    # -- journals ------------------------------------------------------
    def test_get_journal_of_an_unknown_story_is_empty(self):
        store = self.make_store()
        assert store.get_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY) == []

    def test_journals_of_two_stories_stay_separate_and_ordered(self):
        store = self.make_store()
        store.append_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY, {'entry': 1})
        store.append_journal(DEFAULT_NAMESPACE, _PLAN, _OTHER_STORY_KEY, {'entry': 2})
        store.append_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY, {'entry': 3})
        assert store.get_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY) == [
            {'entry': 1},
            {'entry': 3},
        ]
        assert store.get_journal(DEFAULT_NAMESPACE, _PLAN, _OTHER_STORY_KEY) == [{'entry': 2}]

    # -- invalid plan names --------------------------------------------
    def test_get_manifest_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(self, InvalidRecordKeyError, lambda: store.get_manifest(DEFAULT_NAMESPACE, _INVALID_KEY))

    def test_put_manifest_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.put_manifest(DEFAULT_NAMESPACE, _INVALID_KEY, {'version': 1}),
        )

    def test_get_decisions_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(self, InvalidRecordKeyError, lambda: store.get_decisions(DEFAULT_NAMESPACE, _INVALID_KEY))

    def test_append_decision_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.append_decision(DEFAULT_NAMESPACE, _INVALID_KEY, {'decision': 'x'}),
        )

    def test_get_journal_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.get_journal(DEFAULT_NAMESPACE, _INVALID_KEY, _STORY_KEY),
        )

    def test_append_journal_rejects_an_invalid_plan_name(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.append_journal(DEFAULT_NAMESPACE, _INVALID_KEY, _STORY_KEY, {'entry': 1}),
        )

    # -- invalid story keys --------------------------------------------
    def test_get_journal_rejects_an_invalid_story_key(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.get_journal(DEFAULT_NAMESPACE, _PLAN, _INVALID_KEY),
        )

    def test_append_journal_rejects_an_invalid_story_key(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.append_journal(DEFAULT_NAMESPACE, _PLAN, _INVALID_KEY, {'entry': 1}),
        )

    # -- invalid namespaces --------------------------------------------
    def test_get_manifest_rejects_an_invalid_namespace(self):
        store = self.make_store()
        _expect(self, InvalidRecordKeyError, lambda: store.get_manifest(_INVALID_NAMESPACE, _PLAN))

    def test_put_manifest_rejects_an_invalid_namespace(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.put_manifest(_INVALID_NAMESPACE, _PLAN, {'version': 1}),
        )

    # -- argument types ------------------------------------------------
    def test_put_manifest_rejects_a_non_dict_manifest(self):
        store = self.make_store()
        _expect(
            self,
            TypeError,
            lambda: store.put_manifest(DEFAULT_NAMESPACE, _PLAN, ['not', 'a', 'dict']),
        )

    def test_append_decision_rejects_a_non_dict_record(self):
        store = self.make_store()
        _expect(
            self,
            TypeError,
            lambda: store.append_decision(DEFAULT_NAMESPACE, _PLAN, 'not-a-dict'),
        )

    def test_append_journal_rejects_a_non_dict_record(self):
        store = self.make_store()
        _expect(
            self,
            TypeError,
            lambda: store.append_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY, 'not-a-dict'),
        )

    def test_put_manifest_rejects_a_non_json_serializable_manifest(self):
        store = self.make_store()
        _expect(
            self,
            TypeError,
            lambda: store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'tags': {1, 2}}),
        )

    def test_rejected_put_manifest_writes_nothing(self):
        store = self.make_store()
        _expect(
            self,
            InvalidRecordKeyError,
            lambda: store.put_manifest(DEFAULT_NAMESPACE, _INVALID_KEY, {'version': 1}),
        )
        assert store.list_plans(DEFAULT_NAMESPACE) == []


class MultiNamespaceContract:
    """Behaviour of stores that serve several independent namespaces."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def test_same_plan_name_holds_independent_manifests_per_namespace(self):
        store = self.make_store()
        store.put_manifest(_SECOND_NAMESPACE, _PLAN, {'version': 'acme'})
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 'default'})
        assert store.get_manifest(_SECOND_NAMESPACE, _PLAN) == {'version': 'acme'}
        assert store.get_manifest(DEFAULT_NAMESPACE, _PLAN) == {'version': 'default'}

    def test_list_plans_covers_only_its_own_namespace(self):
        store = self.make_store()
        store.put_manifest(_SECOND_NAMESPACE, _PLAN, {'version': 1})
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 2})
        store.put_manifest(DEFAULT_NAMESPACE, 'zzz', {'version': 3})
        assert store.list_plans(_SECOND_NAMESPACE) == [_PLAN]
        assert store.list_plans(DEFAULT_NAMESPACE) == [_PLAN, 'zzz']

    def test_decisions_are_isolated_per_namespace(self):
        store = self.make_store()
        store.append_decision(DEFAULT_NAMESPACE, _PLAN, {'decision': 'default'})
        store.append_decision(_SECOND_NAMESPACE, _PLAN, {'decision': 'acme'})
        assert store.get_decisions(DEFAULT_NAMESPACE, _PLAN) == [{'decision': 'default'}]
        assert store.get_decisions(_SECOND_NAMESPACE, _PLAN) == [{'decision': 'acme'}]

    def test_journals_are_isolated_per_namespace(self):
        store = self.make_store()
        store.append_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY, {'entry': 'default'})
        store.append_journal(_SECOND_NAMESPACE, _PLAN, _STORY_KEY, {'entry': 'acme'})
        assert store.get_journal(DEFAULT_NAMESPACE, _PLAN, _STORY_KEY) == [{'entry': 'default'}]
        assert store.get_journal(_SECOND_NAMESPACE, _PLAN, _STORY_KEY) == [{'entry': 'acme'}]

    def test_reads_from_a_never_written_namespace_are_empty(self):
        store = self.make_store()
        assert store.get_manifest(_SECOND_NAMESPACE, _PLAN) is None
        assert store.get_decisions(_SECOND_NAMESPACE, _PLAN) == []
        assert store.get_journal(_SECOND_NAMESPACE, _PLAN, _STORY_KEY) == []
        assert store.list_plans(_SECOND_NAMESPACE) == []

    def test_should_not_return_another_namespaces_manifests(self):
        store = self.make_store()
        store.put_manifest(_SECOND_NAMESPACE, _PLAN, {'version': 'acme'})
        assert store.get_manifests(DEFAULT_NAMESPACE) == {}
        assert store.get_manifests(_SECOND_NAMESPACE) == {_PLAN: {'version': 'acme'}}


class SingleNamespaceContract:
    """Behaviour of stores that only serve :data:`DEFAULT_NAMESPACE`."""

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    def test_should_refuse_an_unknown_namespace(self):
        store = self.make_store()
        store.put_manifest(DEFAULT_NAMESPACE, _PLAN, {'version': 1})
        _expect(self, UnknownNamespaceError, lambda: store.get_manifests(_SECOND_NAMESPACE))
        assert store.get_manifests(DEFAULT_NAMESPACE) == {_PLAN: {'version': 1}}

    def test_get_manifest_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(self, UnknownNamespaceError, lambda: store.get_manifest(_SECOND_NAMESPACE, _PLAN))

    def test_put_manifest_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: store.put_manifest(_SECOND_NAMESPACE, _PLAN, {'version': 1}),
        )

    def test_list_plans_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(self, UnknownNamespaceError, lambda: store.list_plans(_SECOND_NAMESPACE))

    def test_get_decisions_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(self, UnknownNamespaceError, lambda: store.get_decisions(_SECOND_NAMESPACE, _PLAN))

    def test_append_decision_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: store.append_decision(_SECOND_NAMESPACE, _PLAN, {'decision': 'x'}),
        )

    def test_get_journal_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: store.get_journal(_SECOND_NAMESPACE, _PLAN, _STORY_KEY),
        )

    def test_append_journal_rejects_an_unknown_namespace(self):
        store = self.make_store()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: store.append_journal(_SECOND_NAMESPACE, _PLAN, _STORY_KEY, {'entry': 1}),
        )

    def test_refused_put_manifest_leaves_the_default_namespace_empty(self):
        store = self.make_store()
        _expect(
            self,
            UnknownNamespaceError,
            lambda: store.put_manifest(_SECOND_NAMESPACE, _PLAN, {'version': 1}),
        )
        assert store.list_plans(DEFAULT_NAMESPACE) == []
